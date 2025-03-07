# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Defines Trainer subclasses to perform training on AWS Neuron instances."""

import copy
import math
import os
import shutil
import sys
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import torch
from accelerate import __version__ as accelerate_version
from accelerate.utils import AutocastKwargs, DataLoaderConfiguration, GradientAccumulationPlugin
from packaging import version
from torch.utils.data import Dataset
from transformers import PreTrainedModel, Seq2SeqTrainer, Trainer, TrainingArguments
from transformers.debug_utils import DebugOption, DebugUnderflowOverflow
from transformers.integrations import hp_params
from transformers.modeling_utils import unwrap_model
from transformers.trainer import (
    OPTIMIZER_NAME,
    SCHEDULER_NAME,
    TRAINER_STATE_NAME,
    TRAINING_ARGS_NAME,
)
from transformers.trainer_callback import TrainerState
from transformers.trainer_pt_utils import (
    IterableDatasetShard,
    find_batch_size,
    get_dataloader_sampler,
    nested_concat,
    nested_numpify,
    reissue_pt_warnings,
)
from transformers.trainer_utils import (
    PREFIX_CHECKPOINT_DIR,
    EvalLoopOutput,
    EvalPrediction,
    HPSearchBackend,
    PredictionOutput,
    TrainOutput,
    denumpify_detensorize,
    has_length,
    speed_metrics,
)
from transformers.utils import (
    WEIGHTS_NAME,
    is_accelerate_available,
    is_apex_available,
    is_sagemaker_mp_enabled,
)

from ..utils import logging
from .accelerate import NeuronAccelerator, NeuronDistributedType
from .distributed import Parallelizer, ParallelizersManager
from .distributed.utils import make_optimizer_constructor_lazy
from .training_args import NeuronTrainingArguments
from .utils import (
    is_torch_xla_available,
    patch_within_function,
)
from .utils.cache_utils import (
    get_hf_hub_cache_repos,
    get_model_name_or_path,
    get_neuron_cache_path,
    get_num_neuron_cores_used,
    has_write_access_to_repo,
)
from .utils.hub_cache_utils import ModelCacheEntry, hub_neuronx_cache, patch_neuron_cc_wrapper, synchronize_hub_cache
from .utils.misc import is_main_worker, is_precompilation
from .utils.peft_utils import NeuronPeftModel
from .utils.require_utils import requires_neuronx_distributed, requires_torch_neuronx
from .utils.training_utils import (
    get_model_param_count,
    is_main_worker_for_metrics,
    is_main_worker_for_metrics_method,
    patch_generation_mixin_to_neuron_generation_mixin,
    skip_first_batches,
)
from .utils.version_utils import get_neuronxcc_version


if is_apex_available():
    from apex import amp

if is_torch_xla_available():
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met

if is_sagemaker_mp_enabled():
    from smdistributed.modelparallel import __version__ as SMP_VERSION

    IS_SAGEMAKER_MP_POST_1_10 = version.parse(SMP_VERSION) >= version.parse("1.10")

else:
    IS_SAGEMAKER_MP_POST_1_10 = False

logger = logging.get_logger("transformers.trainer")

KEEP_HF_HUB_PROGRESS_BARS = os.environ.get("KEEP_HF_HUB_PROGRESS_BARS")
if KEEP_HF_HUB_PROGRESS_BARS is None:
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

transformers_get_optimizer_cls_and_kwargs = Trainer.get_optimizer_cls_and_kwargs


class AugmentTrainerForNeuronMixin:
    def __init__(self, *args, **kwargs):
        if not isinstance(self, Trainer):
            raise TypeError(f"{self.__class__.__name__} can only be mixed with Trainer subclasses.")

        training_args = kwargs.get("args", None)
        if training_args is None and len(args) >= 2:
            training_args = args[1]

        self.use_amp = False
        if training_args is not None:
            if training_args.bf16:
                if training_args.half_precision_backend == "amp":
                    self.use_amp = True

        if is_precompilation():
            self.prepare_for_precompilation(training_args)

        super().__init__(*args, **kwargs)

        if not isinstance(self.args, NeuronTrainingArguments):
            raise ValueError(
                f"The NeuronTrainer only accept NeuronTrainingArguments, but {type(self.args)} was provided."
            )

        # We need to change which process can be seen as "world process zero" to make sure the proper metrics
        # (eg.g loss) are logged and sent to the callbacks (for instance WandbCallback).
        self.state = TrainerState(
            is_local_process_zero=self.is_local_process_zero(),
            is_world_process_zero=is_main_worker_for_metrics(),
        )

        if self.args.local_rank <= 0:
            logger.setLevel(logging.INFO)

        # Make the model Neuron-compatible for generation.
        patch_generation_mixin_to_neuron_generation_mixin(self.model)

        # Model cache entry management.
        model_name_or_path_for_cache_entry = get_model_name_or_path(self.model.config)
        model_config_for_cache_entry = copy.deepcopy(self.model.config)
        precision = "bfloat16" if self.accelerator.state.mixed_precision == "bf16" else "float32"
        neuron_config_for_cache_entry = {
            "model_class": self.model.__class__.__name__,
            "precision": precision,
            "num_neuron_cores_per_node": get_num_neuron_cores_used(),
            "compiler_version": get_neuronxcc_version(),
            "tensor_parallel_size": self.args.tensor_parallel_size,
            "pipeline_parallel_size": self.args.pipeline_parallel_size,
        }
        self.model_cache_entry: Optional[ModelCacheEntry] = None
        if model_name_or_path_for_cache_entry is not None:
            model_config_for_cache_entry.neuron = neuron_config_for_cache_entry
            self.model_cache_entry = ModelCacheEntry(model_name_or_path_for_cache_entry, model_config_for_cache_entry)

    @property
    def mp_enabled(self):
        return self.accelerator.distributed_type is NeuronDistributedType.MODEL_PARALLELISM

    def prepare_for_precompilation(self, args: "TrainingArguments"):
        if not is_precompilation():
            return

        if args.num_train_epochs != 1:
            if is_main_worker():
                logger.info("Setting the number of epochs for precompilation to 1.")
            args.num_train_epochs = 1
        if args.do_eval:
            if is_main_worker():
                logger.info("Disabling evaluation during precompilation as this is not well supported yet.")
            args.do_eval = False
        if args.do_predict:
            if is_main_worker():
                logger.info("Disabling prediction during precompilation as this is not well supported yet.")
            args.do_predict = False

    def create_accelerator_and_postprocess(self):
        grad_acc_kwargs = {}
        if is_accelerate_available("0.28.0") and self.args.accelerator_config.gradient_accumulation_kwargs is not None:
            grad_acc_kwargs = self.args.accelerator_config.gradient_accumulation_kwargs

        # check if num_steps is attempted to be passed in gradient_accumulation_kwargs
        if "num_steps" in grad_acc_kwargs and self.args.gradient_accumulation_steps > 1:
            # raise because we do not know which setting is intended.
            raise ValueError(
                "The `AcceleratorConfig`'s `num_steps` is set but `gradient_accumulation_steps` is greater than 1 in the passed `TrainingArguments`"
                "If using the passed `AcceleratorConfig` is desired, do not set the `TrainingArguments` `gradient_accumulation_steps`."
            )
        elif "num_steps" not in grad_acc_kwargs:
            # take the gradient_accumulation_steps setting from TrainingArguments.
            grad_acc_kwargs["num_steps"] = self.args.gradient_accumulation_steps

        grad_acc_kwargs["sync_with_dataloader"] = False

        gradient_accumulation_plugin = GradientAccumulationPlugin(**grad_acc_kwargs)

        accelerator_config = self.args.accelerator_config.to_dict()

        if is_accelerate_available("0.28.0"):
            dataloader_config = DataLoaderConfiguration(
                split_batches=accelerator_config.pop("split_batches"),
                dispatch_batches=accelerator_config.pop("dispatch_batches"),
                even_batches=accelerator_config.pop("even_batches"),
                use_seedable_sampler=accelerator_config.pop("use_seedable_sampler"),
            )
        # this would have been updated above, no need for it anymore
        accelerator_config.pop("gradient_accumulation_kwargs")

        args = {
            "deepspeed_plugin": self.args.deepspeed_plugin,
            "gradient_accumulation_plugin": gradient_accumulation_plugin,
        }
        if is_accelerate_available("0.28.0"):
            args["dataloader_config"] = dataloader_config
        else:
            args.update(accelerator_config)

        # create accelerator object
        self.accelerator = NeuronAccelerator(
            *args,
            mp_plugin=self.args.mp_plugin,
            zero_1=self.args.zero_1,
            mixed_precision="bf16" if self.args.bf16 else "no",
            autocast_backend=self.args.half_precision_backend,
        )

        # some Trainer classes need to use `gather` instead of `gather_for_metrics`, thus we store a flag
        self.gather_function = self.accelerator.gather_for_metrics

        # deepspeed and accelerate flags covering both trainer args and accelerate launcher
        self.is_deepspeed_enabled = getattr(self.accelerator.state, "deepspeed_plugin", None) is not None
        self.is_fsdp_enabled = getattr(self.accelerator.state, "fsdp_plugin", None) is not None

        # post accelerator creation setup
        if self.is_fsdp_enabled:
            fsdp_plugin = self.accelerator.state.fsdp_plugin
            fsdp_plugin.limit_all_gathers = self.args.fsdp_config.get(
                "limit_all_gathers", fsdp_plugin.limit_all_gathers
            )
            if is_accelerate_available("0.23.0"):
                fsdp_plugin.activation_checkpointing = self.args.fsdp_config.get(
                    "activation_checkpointing", fsdp_plugin.activation_checkpointing
                )
                if fsdp_plugin.activation_checkpointing and self.args.gradient_checkpointing:
                    raise ValueError(
                        "The activation_checkpointing in FSDP config and the gradient_checkpointing in training arg "
                        "can't be set to True simultaneously. Please use FSDP's activation_checkpointing logic "
                        "when using FSDP."
                    )

        if self.is_deepspeed_enabled and getattr(self.args, "hf_deepspeed_config", None) is None:
            self.propagate_args_to_deepspeed()

        # `save_only_model` can't be used with DeepSpeed/FSDP along with `load_best_model_at_end`
        if (
            self.args.save_only_model
            and (self.is_deepspeed_enabled or self.is_fsdp_enabled)
            and self.args.load_best_model_at_end
        ):
            wrapper = "DeepSpeed" if self.is_deepspeed_enabled else "FSDP"
            raise ValueError(f"{wrapper} can't be used with `save_only_model` along with `load_best_model_at_end`.")

        # `auto_find_batch_size` isn't yet supported with DeepSpeed/FSDP
        if (self.is_deepspeed_enabled or self.is_fsdp_enabled) and self.args.auto_find_batch_size:
            wrapper = "DeepSpeed" if self.is_deepspeed_enabled else "FSDP"
            raise NotImplementedError(f"`{wrapper}` doesn't support `auto_find_batch_size`.")

    @requires_torch_neuronx
    def synchronize_hub_cache(self):
        repo_id = get_hf_hub_cache_repos()[0]
        if not self.args.skip_cache_push and xm.get_ordinal() == 0:
            has_write_access = has_write_access_to_repo(repo_id)
            if has_write_access:
                cache_path = get_neuron_cache_path()
                synchronize_hub_cache(cache_path=cache_path, cache_repo_id=repo_id)
        xm.rendezvous("Hub cache synchronization done")

    def _wrap_model(self, model, training=True, dataloader=None):
        return super()._wrap_model(
            self.accelerator.patch_model_for_neuron(model), training=training, dataloader=dataloader
        )

    def _get_train_sampler(self) -> Optional[torch.utils.data.Sampler]:
        if self.mp_enabled:
            if self.train_dataset is None or not has_length(self.train_dataset):
                return None

            if self.args.group_by_length:
                raise ValueError("LengthGroupedSampler is currently not supported with model parallelism.")

            return torch.utils.data.RandomSampler(self.train_dataset)
        return super()._get_train_sampler()

    def _get_eval_sampler(self, eval_dataset: torch.utils.data.Dataset) -> Optional[torch.utils.data.Sampler]:
        return torch.utils.data.SequentialSampler(eval_dataset)

    def get_num_trainable_parameters(self):
        return get_model_param_count(self.model, trainable_only=True)

    @staticmethod
    def get_optimizer_cls_and_kwargs(
        args: TrainingArguments, model: Optional[PreTrainedModel] = None
    ) -> Tuple[Any, Any]:
        optimizer_cls, optimizer_kwargs = transformers_get_optimizer_cls_and_kwargs(args, model=model)
        lazy_load = args.mp_plugin.should_parallelize or args.zero_1
        if lazy_load:
            optimizer_cls = make_optimizer_constructor_lazy(optimizer_cls)
        return optimizer_cls, optimizer_kwargs

    @patch_within_function(("transformers.Trainer.get_optimizer_cls_and_kwargs", get_optimizer_cls_and_kwargs))
    def create_optimizer(self):
        return super().create_optimizer()

    def _prepare_input(self, data: Union[torch.Tensor, Any]) -> Union[torch.Tensor, Any]:
        # When pipeline parallelism is enabled, we should not put any tensor on device.
        # It is handled by the NxDPPModel class.
        if self.args.mp_plugin.pipeline_parallel_size > 1:
            return data
        return super()._prepare_input(data)

    def _update_input_specs_in_model_cache_entry(self, input_specs: Dict[str, Any]):
        if self.model_cache_entry is None:
            return
        self.model_cache_entry.config["neuron"]["training"] = self.model.training
        self.model_cache_entry.config["neuron"]["input_specs"] = input_specs

    def _prepare_inputs(self, inputs: Dict[str, Union[torch.Tensor, Any]]) -> Dict[str, Union[torch.Tensor, Any]]:
        inputs = super()._prepare_inputs(inputs)
        input_specs_for_cache_entry = {k: v.shape if isinstance(v, torch.Tensor) else v for k, v in inputs.items()}
        self._update_input_specs_in_model_cache_entry(input_specs_for_cache_entry)
        return inputs

    def compute_loss(self, model, inputs, return_outputs: bool = False):
        from neuronx_distributed.pipeline import NxDPPModel

        if isinstance(model, NxDPPModel):
            inputs = self._prepare_inputs(inputs)
            loss = model.run_train(**inputs)
        else:
            loss = super().compute_loss(model, inputs, return_outputs=return_outputs)
        return loss

    def autocast_smart_context_manager(self, cache_enabled: Optional[bool] = True):
        """
        A helper wrapper that creates an appropriate context manager for `autocast` while feeding it the desired
        arguments, depending on the situation.
        """
        autocast_handler = AutocastKwargs(
            enabled=self.accelerator.autocast_handler.enabled,
            cache_enabled=cache_enabled,
        )
        return self.accelerator.autocast(autocast_handler=autocast_handler)

    def training_step(self, model: torch.nn.Module, inputs: Dict[str, Union[torch.Tensor, Any]]) -> torch.Tensor:
        from neuronx_distributed.pipeline import NxDPPModel

        if isinstance(model, NxDPPModel):
            from neuronx_distributed.parallel_layers.parallel_state import (
                get_pipeline_model_parallel_rank,
                get_pipeline_model_parallel_size,
            )

            with self.compute_loss_context_manager():
                loss = self.compute_loss(model, inputs)

            if get_pipeline_model_parallel_rank() != get_pipeline_model_parallel_size() - 1:
                use_bf16 = self.accelerator.state.mixed_precision == "bf16"
                dtype = torch.bfloat16 if use_bf16 else torch.float32
                loss = torch.tensor(0, dtype=dtype).to(xm.xla_device())
            else:
                loss = loss.detach()
            output = loss / self.args.gradient_accumulation_steps
        else:
            output = super().training_step(model, inputs)
        return output

    @requires_neuronx_distributed
    def prediction_step(
        self,
        model: torch.nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        from neuronx_distributed.pipeline import NxDPPModel

        if isinstance(model, NxDPPModel):
            if not prediction_loss_only:
                raise ValueError("Only the prediction loss can be returned when doing pipeline parallelism.")
            loss = model.run_eval(**inputs)
            if loss is None:
                use_bf16 = self.accelerator.state.mixed_precision == "bf16"
                dtype = torch.bfloat16 if use_bf16 else torch.float32
                loss = torch.tensor(0, dtype=dtype).to(xm.xla_device())
            return (loss, None, None)
        return super().prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)

    def _reduce_loss(self, tr_loss: torch.Tensor) -> torch.Tensor:
        from neuronx_distributed.parallel_layers.parallel_state import (
            get_data_parallel_group,
            get_data_parallel_size,
            model_parallel_is_initialized,
        )

        if model_parallel_is_initialized():
            dp_size = get_data_parallel_size()
        else:
            dp_size = xm.xrt_world_size()

        tr_loss_div = tr_loss / dp_size

        if self.args.mp_plugin.should_parallelize:
            # It works even for PP because under PP we make it so that the main process to log for callbacks is
            # the one on dp_rank = tp_rank = 0 and pp_rank = pp_size -1.
            reduced_tr_loss = xm.all_reduce(xm.REDUCE_SUM, tr_loss_div, groups=get_data_parallel_group(as_list=True))
        else:
            reduced_tr_loss = xm.all_reduce(xm.REDUCE_SUM, tr_loss_div)

        return reduced_tr_loss

    def _maybe_log_save_evaluate(self, tr_loss, grad_norm, model, trial, epoch, ignore_keys_for_eval):
        # We always reduce the loss, even when we do not use it to avoid a new graph.
        # This communication is not costly.
        reduced_tr_loss = self._reduce_loss(tr_loss)

        if self.control.should_log and self.state.global_step > self._globalstep_last_logged:
            if isinstance(getattr(self, "_zero_loss_value"), torch.Tensor):
                tr_loss.data = self._zero_loss_value.data
            else:
                tr_loss.zero_()

            def log_closure(self, reduced_tr_loss, grad_norm):
                if is_main_worker_for_metrics():
                    logs: Dict[str, float] = {}
                    tr_loss_scalar = reduced_tr_loss.to("cpu").item()

                    logs["loss"] = round(tr_loss_scalar / (self.state.global_step - self._globalstep_last_logged), 4)
                    logs["learning_rate"] = self._get_learning_rate()

                    if grad_norm is not None:
                        logs["grad_norm"] = (
                            grad_norm.detach().to("cpu").item() if isinstance(grad_norm, torch.Tensor) else grad_norm
                        )

                    self._total_loss_scalar += tr_loss_scalar
                    self._globalstep_last_logged = self.state.global_step
                    self.store_flos()
                    self.log(logs)

            xm.add_step_closure(log_closure, (self, reduced_tr_loss, grad_norm))

        metrics = None
        if self.control.should_evaluate:
            metrics = self.evaluate(ignore_keys=ignore_keys_for_eval)
            self._report_to_hp_search(trial, self.state.global_step, metrics)

            # Run delayed LR scheduler now that metrics are populated
            if isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                metric_to_check = self.args.metric_for_best_model
                if not metric_to_check.startswith("eval_"):
                    metric_to_check = f"eval_{metric_to_check}"
                self.lr_scheduler.step(metrics[metric_to_check])

        if self.control.should_save:

            def save_closure(self, model, trial, metrics):
                self._save_checkpoint(model, trial, metrics=metrics)
                self.control = self.callback_handler.on_save(self.args, self.state, self.control)

            xm.add_step_closure(save_closure, (self, model, trial, metrics))

    def _save_xla(self, output_dir: Optional[str] = None):
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        if is_main_worker():
            logger.info(f"Saving model checkpoint to {output_dir}")

            os.makedirs(output_dir, exist_ok=True)
            torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))

        # Save a trained model and configuration using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        xm.rendezvous("saving_checkpoint")
        if (
            not isinstance(self.model, NeuronPeftModel)
            and self.accelerator.distributed_type is NeuronDistributedType.MODEL_PARALLELISM
        ):
            if is_main_worker():
                logger.info(
                    "Model parallelism is enabled, saving the model sharded state dict instead of the full state dict."
                )
            # TODO: how to handle pp?
            if isinstance(self.model, PreTrainedModel):
                from neuronx_distributed.parallel_layers.parallel_state import get_tensor_model_parallel_size

                config = copy.deepcopy(self.model.config)
                if self.args.mp_plugin.parallelize_embeddings:
                    config.vocab_size = config.vocab_size * get_tensor_model_parallel_size()
                config.save_pretrained(output_dir)

            # This mark_step is needed to avoid hang issues.
            xm.mark_step()
            Parallelizer.save_model_sharded_checkpoint(
                self.model,
                output_dir,
                optimizer=self.optimizer if not self.args.save_only_model else None,
                use_xser=self.accelerator.state.mp_plugin.use_xser,
                async_save=self.accelerator.state.mp_plugin.async_save,
                num_local_ranks_per_step=self.accelerator.state.mp_plugin.num_local_ranks_per_step,
            )
        else:
            supported_classes = (PreTrainedModel, NeuronPeftModel)
            if not isinstance(self.model, supported_classes):
                if isinstance(unwrap_model(self.model), supported_classes):
                    kwargs = (
                        {}
                        if isinstance(unwrap_model(self.model), PreTrainedModel)
                        else {"async_save": self.args.async_save}
                    )
                    unwrap_model(self.model).save_pretrained(
                        output_dir,
                        is_main_process=self.args.should_save,
                        state_dict=self.model.state_dict(),
                        save_function=xm.save,
                        **kwargs,
                    )
                else:
                    if is_main_worker():
                        logger.info("Trainer.model is not a `PreTrainedModel`, only saving its state dict.")
                    state_dict = self.model.state_dict()
                    xm.save(state_dict, os.path.join(output_dir, WEIGHTS_NAME))
            else:
                kwargs = {} if isinstance(self.model, PreTrainedModel) else {"async_save": self.args.async_save}
                self.model.save_pretrained(
                    output_dir, is_main_process=self.args.should_save, save_function=xm.save, **kwargs
                )

        if self.tokenizer is not None and self.args.should_save:
            self.tokenizer.save_pretrained(output_dir)

    def save_model(self, output_dir: Optional[str] = None, _internal_call: bool = False):
        if not is_precompilation():  # Avoid unnecessary model saving during precompilation
            with patch_neuron_cc_wrapper():
                if self.model_cache_entry is not None and "input_specs" not in self.model_cache_entry.config["neuron"]:
                    model_cache_entry = None
                else:
                    model_cache_entry = self.model_cache_entry
                with hub_neuronx_cache("training", entry=model_cache_entry):
                    if output_dir is None:
                        output_dir = self.args.output_dir

                    self._save_xla(output_dir)

            # Push to the Hub when `save_model` is called by the user.
            if self.args.push_to_hub and not _internal_call:
                self.push_to_hub(commit_message="Model save")
        elif is_main_worker():
            logger.info("Skipping trainer.save_model() while running under neuron_parallel_compile")

    def _save_checkpoint(self, model, trial, metrics=None):
        # In all cases, including ddp/dp/deepspeed, self.model is always a reference to the model we
        # want to save except FullyShardedDDP.
        # assert unwrap_model(model) is self.model, "internal model should be a reference to self.model"

        # Save model checkpoint
        checkpoint_folder = f"{PREFIX_CHECKPOINT_DIR}-{self.state.global_step}"

        if self.hp_search_backend is None and trial is None:
            self.store_flos()

        run_dir = self._get_output_dir(trial=trial)
        output_dir = os.path.join(run_dir, checkpoint_folder)

        if xm.is_master_ordinal():
            os.makedirs(output_dir, exist_ok=True)

        self.save_model(output_dir, _internal_call=True)

        # The optimizer state is saved in the shard alongside with the model parameters when doing model-parallelism.
        if self.accelerator.distributed_type is not NeuronDistributedType.MODEL_PARALLELISM:
            xm.rendezvous("saving_optimizer_states")
            xm.save(self.optimizer.state_dict(), os.path.join(output_dir, OPTIMIZER_NAME))

            if not self.args.save_only_model:
                # Save optimizer and scheduler
                self._save_optimizer_and_scheduler(output_dir)

        with warnings.catch_warnings(record=True) as caught_warnings:
            xm.save(self.lr_scheduler.state_dict(), os.path.join(output_dir, SCHEDULER_NAME))
            reissue_pt_warnings(caught_warnings)

        if not self.args.save_only_model:
            # Save RNG state
            self._save_rng_state(output_dir)

        # Determine the new best metric / best model checkpoint
        if metrics is not None and self.args.metric_for_best_model is not None:
            metric_to_check = self.args.metric_for_best_model
            if not metric_to_check.startswith("eval_"):
                metric_to_check = f"eval_{metric_to_check}"
            metric_value = metrics[metric_to_check]

            operator = np.greater if self.args.greater_is_better else np.less
            if (
                self.state.best_metric is None
                or self.state.best_model_checkpoint is None
                or operator(metric_value, self.state.best_metric)
            ):
                self.state.best_metric = metric_value
                self.state.best_model_checkpoint = output_dir

        # Save the Trainer state
        if self.args.should_save:
            self.state.save_to_json(os.path.join(output_dir, TRAINER_STATE_NAME))

        # A process can arrive here before the process 0 has a chance to save the model, in which case output_dir may
        # not yet exist.
        os.makedirs(output_dir, exist_ok=True)

        if self.args.push_to_hub:
            self._push_from_checkpoint(output_dir)

        # Maybe delete some older checkpoints.
        if self.args.should_save:
            self._rotate_checkpoints(use_mtime=True, output_dir=run_dir)

    def _load_from_checkpoint(self, resume_from_checkpoint, model=None):
        # It has been handled during model parallelization.
        if self.accelerator.distributed_type is NeuronDistributedType.MODEL_PARALLELISM:
            return
        super()._load_from_checkpoint(resume_from_checkpoint, model=model)

    def _load_optimizer_and_scheduler(self, checkpoint):
        if checkpoint is None:
            return
        if self.accelerator.distributed_type is NeuronDistributedType.MODEL_PARALLELISM:
            lr_scheduler_state = torch.load(os.path.join(checkpoint, SCHEDULER_NAME), map_location="cpu")
            xm.send_cpu_data_to_device(lr_scheduler_state, self.args.device)
            self.lr_scheduler.load_state_dict(lr_scheduler_state)

            parallelizer = ParallelizersManager.parallelizer_for_model(self.model)
            parallelizer.load_optimizer_sharded_checkpoint(self.optimizer, checkpoint)
        else:
            return super()._load_optimizer_and_scheduler(checkpoint)

    @requires_neuronx_distributed
    def _inner_training_loop(
        self, batch_size=None, args=None, resume_from_checkpoint=None, trial=None, ignore_keys_for_eval=None
    ):
        from neuronx_distributed.pipeline import NxDPPModel

        self.accelerator.free_memory()
        self._train_batch_size = batch_size
        if is_main_worker():
            logger.debug(f"Currently training with a batch size of: {self._train_batch_size}")

        # Data loader and number of training steps
        train_dataloader = self.get_train_dataloader()

        # Setting up training control variables:
        # number of training epochs: num_train_epochs
        # number of training steps per epoch: num_update_steps_per_epoch
        # total number of training steps to execute: max_steps
        total_train_batch_size = self._train_batch_size * args.gradient_accumulation_steps * args.dp_size

        len_dataloader = None
        num_train_tokens = None
        if has_length(train_dataloader):
            len_dataloader = len(train_dataloader)
            num_update_steps_per_epoch = len_dataloader // args.gradient_accumulation_steps
            num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
            num_examples = self.num_examples(train_dataloader)
            if args.max_steps > 0:
                max_steps = args.max_steps
                num_train_epochs = args.max_steps // num_update_steps_per_epoch + int(
                    args.max_steps % num_update_steps_per_epoch > 0
                )
                # May be slightly incorrect if the last batch in the training dataloader has a smaller size but it's
                # the best we can do.
                num_train_samples = args.max_steps * total_train_batch_size
                if args.include_tokens_per_second:
                    num_train_tokens = (
                        self.num_tokens(train_dataloader, args.max_steps) * args.gradient_accumulation_steps
                    )
            else:
                max_steps = math.ceil(args.num_train_epochs * num_update_steps_per_epoch)
                num_train_epochs = math.ceil(args.num_train_epochs)
                num_train_samples = self.num_examples(train_dataloader) * args.num_train_epochs
                if args.include_tokens_per_second:
                    num_train_tokens = self.num_tokens(train_dataloader) * args.num_train_epochs
        elif args.max_steps > 0:  # Rely on max_steps when dataloader does not have a working size
            max_steps = args.max_steps
            # Setting a very large number of epochs so we go as many times as necessary over the iterator.
            num_train_epochs = sys.maxsize
            num_update_steps_per_epoch = max_steps
            num_examples = total_train_batch_size * args.max_steps
            num_train_samples = args.max_steps * total_train_batch_size
            if args.include_tokens_per_second:
                num_train_tokens = self.num_tokens(train_dataloader, args.max_steps) * args.gradient_accumulation_steps
        else:
            raise ValueError(
                "args.max_steps must be set to a positive value if dataloader does not have a length, was"
                f" {args.max_steps}"
            )

        if DebugOption.UNDERFLOW_OVERFLOW in self.args.debug:
            if self.args.n_gpu > 1:
                # nn.DataParallel(model) replicates the model, creating new variables and module
                # references registered here no longer work on other gpus, breaking the module
                raise ValueError(
                    "Currently --debug underflow_overflow is not supported under DP. Please use DDP"
                    " (torch.distributed.launch)."
                )
            else:
                debug_overflow = DebugUnderflowOverflow(self.model)  # noqa

        delay_optimizer_creation = is_sagemaker_mp_enabled() or self.is_fsdp_xla_enabled or self.is_fsdp_enabled

        # We need to reset the scheduler, as its parameters may be different on subsequent calls
        if self._created_lr_scheduler:
            self.lr_scheduler = None
            self._created_lr_scheduler = False

        if not delay_optimizer_creation:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        self.state = TrainerState()
        self.state.is_hyper_param_search = trial is not None

        # Compute absolute values for logging, eval, and save if given as ratio
        if args.logging_steps is not None:
            if args.logging_steps < 1:
                self.state.logging_steps = math.ceil(max_steps * args.logging_steps)
            else:
                self.state.logging_steps = args.logging_steps
        if args.eval_steps is not None:
            if args.eval_steps < 1:
                self.state.eval_steps = math.ceil(max_steps * args.eval_steps)
            else:
                self.state.eval_steps = args.eval_steps
        if args.save_steps is not None:
            if args.save_steps < 1:
                self.state.save_steps = math.ceil(max_steps * args.save_steps)
            else:
                self.state.save_steps = args.save_steps

        # Activate gradient checkpointing if needed
        # It is handled differently if pipeline parallelism is enabled.
        if args.gradient_checkpointing and args.pipeline_parallel_size == 1:
            if args.gradient_checkpointing_kwargs is None:
                gradient_checkpointing_kwargs = {}
            else:
                gradient_checkpointing_kwargs = args.gradient_checkpointing_kwargs

            self.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs=gradient_checkpointing_kwargs)

        model = self._wrap_model(self.model_wrapped)

        # as the model is wrapped, don't use `accelerator.prepare`
        # this is for unhandled cases such as
        # FSDP-XLA, SageMaker MP/DP, DataParallel, IPEX
        use_accelerator_prepare = True if model is self.model else False

        if delay_optimizer_creation:
            if use_accelerator_prepare:
                self.model = self.accelerator.prepare(self.model)
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        # prepare using `accelerator` prepare
        if use_accelerator_prepare:
            self.model.train()
            if hasattr(self.lr_scheduler, "step"):
                if self.use_apex:
                    model = self.accelerator.prepare(self.model)
                else:
                    model, self.optimizer = self.accelerator.prepare(self.model, self.optimizer)
            else:
                # to handle cases wherein we pass "DummyScheduler" such as when it is specified in DeepSpeed config.
                model, self.optimizer, self.lr_scheduler = self.accelerator.prepare(
                    self.model, self.optimizer, self.lr_scheduler
                )

        if isinstance(model, NxDPPModel):
            self.model = model

        if self.is_fsdp_enabled:
            self.model = self.model_wrapped = model

        # for the rest of this function `model` is the outside model, whether it was wrapped or not
        if model is not self.model:
            self.model_wrapped = model

        # Check if saved optimizer or scheduler states exist
        self._load_optimizer_and_scheduler(resume_from_checkpoint)

        # important: at this point:
        # self.model         is the Transformers Model
        # self.model_wrapped is DDP(Transformers Model), Deepspeed(Transformers Model),
        # FSDP(Transformers Model), Dynamo Optimized Module(Transformers Model) etc.

        # Train!
        parameter_count = get_model_param_count(model, trainable_only=True)
        if is_main_worker():
            logger.info("***** Running training *****")
            logger.info(f"  Num examples = {num_examples:,}")
            logger.info(f"  Num Epochs = {num_train_epochs:,}")
            logger.info(f"  Instantaneous batch size per device = {self.args.per_device_train_batch_size:,}")
            if self.args.per_device_train_batch_size != self._train_batch_size:
                logger.info(
                    f"  Training with DataParallel so batch size has been adjusted to: {self._train_batch_size:,}"
                )
            logger.info(
                f"  Total train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size:,}"
            )
            logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
            logger.info(f"  Total optimization steps = {max_steps:,}")
            logger.info(f"  Number of trainable parameters = {parameter_count:,}")

        self.state.epoch = 0
        start_time = time.time()
        epochs_trained = 0
        steps_trained_in_current_epoch = 0
        steps_trained_progress_bar = None

        # Check if continuing training from a checkpoint
        if resume_from_checkpoint is not None and os.path.isfile(
            os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME)
        ):
            self.state = TrainerState.load_from_json(os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME))
            self.compare_trainer_and_checkpoint_args(self.args, self.state)
            epochs_trained = self.state.global_step // num_update_steps_per_epoch
            if not args.ignore_data_skip:
                steps_trained_in_current_epoch = self.state.global_step % (num_update_steps_per_epoch)
                steps_trained_in_current_epoch *= args.gradient_accumulation_steps
            else:
                steps_trained_in_current_epoch = 0

            if is_main_worker():
                logger.info("  Continuing training from checkpoint, will skip to saved global_step")
                logger.info(f"  Continuing training from epoch {epochs_trained}")
                logger.info(f"  Continuing training from global step {self.state.global_step}")
                if not args.ignore_data_skip:
                    logger.info(
                        f"  Will skip the first {epochs_trained} epochs then the first"
                        f" {steps_trained_in_current_epoch} batches in the first epoch."
                    )

        # Update the references
        self.callback_handler.model = self.model
        self.callback_handler.optimizer = self.optimizer
        self.callback_handler.lr_scheduler = self.lr_scheduler
        self.callback_handler.train_dataloader = train_dataloader
        if self.hp_name is not None and self._trial is not None:
            # use self._trial because the SigOpt/Optuna hpo only call `_hp_search_setup(trial)` instead of passing trial
            # parameter to Train when using DDP.
            self.state.trial_name = self.hp_name(self._trial)
        if trial is not None:
            assignments = trial.assignments if self.hp_search_backend == HPSearchBackend.SIGOPT else trial
            self.state.trial_params = hp_params(assignments)
        else:
            self.state.trial_params = None
        # This should be the same if the state has been saved but in case the training arguments changed, it's safer
        # to set this after the load.
        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        # We need to change which process can be seen as "world process zero" to make sure the proper metrics
        # (eg.g loss) are logged and sent to the callbacks (for instance WandbCallback).
        self.state.is_world_process_zero = is_main_worker_for_metrics()

        # tr_loss is a tensor to avoid synchronization of TPUs through .item()
        tr_loss = torch.tensor(0.0).to(args.device)
        # _total_loss_scalar is updated everytime .item() has to be called on tr_loss and stores the sum of all losses
        self._total_loss_scalar = 0.0
        self._globalstep_last_logged = self.state.global_step

        self.optimizer.zero_grad()
        grad_norm: Optional[float] = None
        self.control = self.callback_handler.on_train_begin(args, self.state, self.control)

        # Mark step before training to materialize any tensor before creating the training graph.
        xm.mark_step()

        # Skip the first epochs_trained epochs to get the random state of the dataloader at the right point.
        if not args.ignore_data_skip:
            for epoch in range(epochs_trained):
                sampler = get_dataloader_sampler(train_dataloader)
                sampler_kinds = [torch.utils.data.RandomSampler]
                if version.parse(accelerate_version) > version.parse("0.23.0"):
                    from accelerate.data_loader import SeedableRandomSampler

                    sampler_kinds.append(SeedableRandomSampler)
                is_random_sampler = isinstance(sampler, tuple(sampler_kinds))
                if not is_random_sampler:
                    # We just need to begin an iteration to create the randomization of the sampler.
                    for _ in train_dataloader:
                        break
                else:
                    # Otherwise we need to call the whooooole sampler cause there is some random operation added
                    # AT THE VERY END!
                    sampler = sampler if sampler is not None else []
                    _ = list(sampler)

        total_batched_samples = 0
        for epoch in range(epochs_trained, num_train_epochs):
            epoch_iterator = train_dataloader
            if hasattr(epoch_iterator, "set_epoch"):
                epoch_iterator.set_epoch(epoch)

            # Reset the past mems state at the beginning of each epoch if necessary.
            if args.past_index >= 0:
                self._past = None

            steps_in_epoch = (
                len(epoch_iterator)
                if len_dataloader is not None
                else args.max_steps * args.gradient_accumulation_steps
            )
            self.control = self.callback_handler.on_epoch_begin(args, self.state, self.control)

            if epoch == epochs_trained and resume_from_checkpoint is not None and steps_trained_in_current_epoch == 0:
                self._load_rng_state(resume_from_checkpoint)

            rng_to_sync = False
            steps_skipped = 0
            if steps_trained_in_current_epoch > 0:
                epoch_iterator = skip_first_batches(epoch_iterator, steps_trained_in_current_epoch)
                steps_skipped = steps_trained_in_current_epoch
                steps_trained_in_current_epoch = 0
                rng_to_sync = True

            step = -1
            for step, inputs in enumerate(epoch_iterator):
                total_batched_samples += 1

                if self.args.include_num_input_tokens_seen:
                    main_input_name = getattr(self.model, "main_input_name", "input_ids")
                    if main_input_name not in inputs:
                        logger.warning(
                            "Tried to track the number of tokens seen, however the current model is "
                            "not configured properly to know what item is the input. To fix this, add "
                            "a `main_input_name` attribute to the model class you are using."
                        )
                    else:
                        input_device = inputs[main_input_name].device
                        self.state.num_input_tokens_seen += torch.sum(
                            self.accelerator.gather(
                                torch.tensor(inputs[main_input_name].numel(), device=input_device, dtype=torch.int64)
                            )
                        ).item()

                if rng_to_sync:
                    self._load_rng_state(resume_from_checkpoint)
                    rng_to_sync = False

                # Skip past any already trained steps if resuming training
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    if steps_trained_progress_bar is not None:
                        steps_trained_progress_bar.update(1)
                    if steps_trained_in_current_epoch == 0:
                        self._load_rng_state(resume_from_checkpoint)
                    continue
                elif steps_trained_progress_bar is not None:
                    steps_trained_progress_bar.close()
                    steps_trained_progress_bar = None

                if step % args.gradient_accumulation_steps == 0:
                    self.control = self.callback_handler.on_step_begin(args, self.state, self.control)

                with self.accelerator.accumulate(model):
                    tr_loss_step = self.training_step(model, inputs)

                if (
                    args.logging_nan_inf_filter
                    and not is_torch_xla_available()
                    and (torch.isnan(tr_loss_step) or torch.isinf(tr_loss_step))
                ):
                    # if loss is nan or inf simply add the average of previous logged losses
                    tr_loss += tr_loss / (1 + self.state.global_step - self._globalstep_last_logged)
                else:
                    if tr_loss.device != tr_loss_step.device:
                        raise ValueError(
                            f"Calculated loss must be on the original device: {tr_loss.device} but device in use is "
                            f"{tr_loss_step.device}"
                        )
                    tr_loss += tr_loss_step

                self.current_flos += float(self.floating_point_ops(inputs))

                is_last_step_and_steps_less_than_grad_acc = (
                    steps_in_epoch <= args.gradient_accumulation_steps and (step + 1) == steps_in_epoch
                )

                if (
                    total_batched_samples % args.gradient_accumulation_steps == 0
                    or
                    # last step in epoch but step is always smaller than gradient_accumulation_steps
                    is_last_step_and_steps_less_than_grad_acc
                ):
                    xm.mark_step()

                    # Gradient clipping
                    if args.max_grad_norm is not None and args.max_grad_norm > 0:
                        # deepspeed does its own clipping

                        if is_sagemaker_mp_enabled() and args.fp16:
                            self.optimizer.clip_master_grads(args.max_grad_norm)
                            _grad_norm = self.optimizer.clip_master_grads(args.max_grad_norm)
                        elif self.use_apex:
                            # Revert to normal clipping otherwise, handling Apex or full precision
                            torch.nn.utils.clip_grad_norm_(
                                amp.master_params(self.optimizer),
                                args.max_grad_norm,
                            )
                            _grad_norm = torch.nn.utils.clip_grad_norm_(
                                amp.master_params(self.optimizer),
                                args.max_grad_norm,
                            )
                        else:
                            _grad_norm = self.accelerator.clip_grad_norm_(
                                model.parameters(),
                                args.max_grad_norm,
                            )
                        grad_norm = _grad_norm

                    # Optimizer step
                    self.optimizer.step()
                    optimizer_was_run = not self.accelerator.optimizer_step_was_skipped
                    if optimizer_was_run:
                        # Delay optimizer scheduling until metrics are generated
                        if not isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                            self.lr_scheduler.step()
                    self.optimizer.zero_grad()

                    self.state.global_step += 1
                    self.state.epoch = epoch + (step + 1 + steps_skipped) / steps_in_epoch
                    self.control = self.callback_handler.on_step_end(args, self.state, self.control)

                    # `_zero_loss_value` is used to reset the value of `tr_loss`.
                    # By doing that, we do not have to do `tr_loss.zero_()` when logging the loss.
                    # This way we do not insert a new op in the XLA graph (for `tr_loss.zero_()`) which woud create
                    # multiple graphs depending on the fact that we are logging or not.
                    # Here we always create a scalar whose value is `0.0`, this way the graph stays the same whether or
                    # not we are logging. The only difference when logging is that we set
                    # `tr_loss.data = self._zero_loss_value.data`, which should not create new graph ops.
                    self._zero_loss_value = torch.tensor(0.0, device=args.device)
                    self._maybe_log_save_evaluate(tr_loss, grad_norm, model, trial, epoch, ignore_keys_for_eval)
                else:
                    self.control = self.callback_handler.on_substep_end(args, self.state, self.control)

                if self.control.should_epoch_stop or self.control.should_training_stop:
                    # PyTorch/XLA relies on the data loader to insert the mark_step for
                    # each step. Since we are breaking the loop early, we need to manually
                    # insert the mark_step here.
                    if is_torch_xla_available():
                        xm.mark_step()
                    break
            if step < 0:
                if is_main_worker():
                    logger.warning(
                        "There seems to be not a single sample in your epoch_iterator, stopping training at step"
                        f" {self.state.global_step}! This is expected if you're using an IterableDataset and set"
                        f" num_steps ({max_steps}) higher than the number of available samples."
                    )
                self.control.should_training_stop = True

            self.control = self.callback_handler.on_epoch_end(args, self.state, self.control)
            self._maybe_log_save_evaluate(tr_loss, grad_norm, model, trial, epoch, ignore_keys_for_eval)

            if DebugOption.TPU_METRICS_DEBUG in self.args.debug:
                if is_torch_xla_available():
                    # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
                    xm.master_print(met.metrics_report())
                else:
                    logger.warning(
                        "You enabled PyTorch/XLA debug metrics but you don't have a TPU "
                        "configured. Check your training configuration if this is unexpected."
                    )
            if self.control.should_training_stop:
                break

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        if is_main_worker():
            logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        if args.load_best_model_at_end and self.state.best_model_checkpoint is not None:
            # Wait for everyone to get here so we are sure the model has been saved by process 0.
            xm.rendezvous("load_best_model")

            self._load_best_model()

        # add remaining tr_loss
        loss_scalar = tr_loss.to("cpu").item()
        self._total_loss_scalar += loss_scalar
        effective_global_step = max(self.state.global_step, 0.001)  # Avoid ZeroDivisionError
        train_loss = self._total_loss_scalar / effective_global_step

        metrics = speed_metrics(
            "train",
            start_time,
            num_samples=num_train_samples,
            num_steps=self.state.max_steps,
            num_tokens=num_train_tokens,
        )
        self.store_flos()
        metrics["total_flos"] = self.state.total_flos
        metrics["train_loss"] = train_loss

        self.is_in_train = False

        self._memory_tracker.stop_and_update_metrics(metrics)

        if is_main_worker_for_metrics():
            self.log(metrics)

        run_dir = self._get_output_dir(trial)
        checkpoints_sorted = self._sorted_checkpoints(use_mtime=False, output_dir=run_dir)

        # Delete the last checkpoint when save_total_limit=1 if it's different from the best checkpoint and process allowed to save.
        if self.args.should_save and self.state.best_model_checkpoint is not None and self.args.save_total_limit == 1:
            for checkpoint in checkpoints_sorted:
                if not os.path.samefile(checkpoint, self.state.best_model_checkpoint):
                    if is_main_worker():
                        logger.info(f"Deleting older checkpoint [{checkpoint}] due to args.save_total_limit")
                    shutil.rmtree(checkpoint)

        self.control = self.callback_handler.on_train_end(args, self.state, self.control)

        # Wait for the checkpoint to be uploaded.
        self._finish_current_push()

        # After training we make sure to retrieve back the original forward pass method
        # for the embedding layer by removing the forward post hook.
        if self.neftune_noise_alpha is not None:
            self._deactivate_neftune(self.model)

        return TrainOutput(self.state.global_step, train_loss, metrics)

    @requires_neuronx_distributed
    def evaluation_loop(
        self,
        dataloader: torch.utils.data.DataLoader,
        description: str,
        prediction_loss_only: Optional[bool] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Prediction/evaluation loop, shared by `Trainer.evaluate()` and `Trainer.predict()`.

        Works both with or without labels.
        """
        from neuronx_distributed.parallel_layers.parallel_state import get_data_parallel_size
        from neuronx_distributed.pipeline import NxDPPModel

        # This will prepare the model if it was not prepared before.
        # This is needed for example for TP when we performing only evaluation (no training):
        #   1. The model needs to be loaded if it was lazy loaded.
        #   2. The model needs to be parallelized.
        model = self.accelerator.prepare_model(self.model)

        args = self.args

        prediction_loss_only = prediction_loss_only if prediction_loss_only is not None else args.prediction_loss_only

        is_nxdppmodel = isinstance(model, NxDPPModel)
        if not is_nxdppmodel:
            model = self._wrap_model(model, training=False, dataloader=dataloader)

        if len(self.accelerator._models) == 0 and model is self.model:
            model = (
                self.accelerator.prepare(model)
                if self.is_deepspeed_enabled
                else self.accelerator.prepare_model(model, evaluation_mode=True)
            )

            if self.is_fsdp_enabled:
                self.model = model

            # for the rest of this function `model` is the outside model, whether it was wrapped or not
            if model is not self.model:
                self.model_wrapped = model

            # backward compatibility
            if self.is_deepspeed_enabled:
                self.deepspeed = self.model_wrapped

        # if full fp16 or bf16 eval is wanted and this ``evaluation`` or ``predict`` isn't called
        # while ``train`` is running, cast it to the right dtype first and then put on device
        if not self.is_in_train and not is_nxdppmodel:
            if args.fp16_full_eval:
                model = model.to(dtype=torch.float16, device=args.device)
            elif args.bf16_full_eval:
                model = model.to(dtype=torch.bfloat16, device=args.device)

        batch_size = self.args.eval_batch_size

        if is_main_worker():
            logger.info(f"***** Running {description} *****")
            dp_size = get_data_parallel_size()
            logger.info(f"  Num data parallel workers = {dp_size}")
            if has_length(dataloader):
                num_examples = self.num_examples(dataloader)
                total_num_examples = num_examples * dp_size
                logger.info(f"  Per data parallel worker num examples = {num_examples}")
                logger.info(f"  Total num examples = {total_num_examples}")
            else:
                logger.info("  Num examples: Unknown")
            logger.info(f"  Batch size = {batch_size}")

        if not is_nxdppmodel:
            model.eval()

        self.callback_handler.eval_dataloader = dataloader
        # Do this before wrapping.
        eval_dataset = getattr(dataloader, "dataset", None)

        if args.past_index >= 0:
            self._past = None

        # Initialize containers
        # losses/preds/labels on GPU/TPU (accumulated for eval_accumulation_steps)
        losses_host = None
        preds_host = None
        labels_host = None
        inputs_host = None

        # losses/preds/labels on CPU (final containers)
        all_losses = None
        all_preds = None
        all_labels = None
        all_inputs = None
        # Will be useful when we have an iterable dataset so don't know its length.

        observed_num_examples = 0
        # Main evaluation loop
        for step, inputs in enumerate(dataloader):
            # Update the observed num examples
            observed_batch_size = find_batch_size(inputs)
            if observed_batch_size is not None:
                observed_num_examples += observed_batch_size
                # For batch samplers, batch_size is not known by the dataloader in advance.
                if batch_size is None:
                    batch_size = observed_batch_size

            if is_nxdppmodel and observed_batch_size % model.num_microbatches != 0:
                if is_main_worker() == 0:
                    logger.warning(
                        "Skipping the evaluation step because the pipeline number of microbatches "
                        f"({model.num_microbatches}) does not divide the batch size ({observed_batch_size})."
                    )
                continue

            # Prediction step
            loss, logits, labels = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)
            main_input_name = getattr(model, "main_input_name", "input_ids")
            inputs_decode = self._prepare_input(inputs[main_input_name]) if args.include_inputs_for_metrics else None

            xm.mark_step()

            # Update containers on host
            if loss is not None:
                losses = self.accelerator.gather_for_metrics((loss.repeat(batch_size)))
                losses_host = losses if losses_host is None else nested_concat(losses_host, losses, padding_index=-100)
            if labels is not None:
                labels = self.accelerator.pad_across_processes(labels, dim=1, pad_index=-100)
            if inputs_decode is not None:
                inputs_decode = self.accelerator.pad_across_processes(inputs_decode, dim=1, pad_index=-100)
                inputs_decode = self.accelerator.gather_for_metrics((inputs_decode))
                inputs_host = (
                    inputs_decode
                    if inputs_host is None
                    else nested_concat(inputs_host, inputs_decode, padding_index=-100)
                )
            if logits is not None:
                logits = self.accelerator.pad_across_processes(logits, dim=1, pad_index=-100)
                if self.preprocess_logits_for_metrics is not None:
                    logits = self.preprocess_logits_for_metrics(logits, labels)
                logits = self.accelerator.gather_for_metrics((logits))
                preds_host = logits if preds_host is None else nested_concat(preds_host, logits, padding_index=-100)

            if labels is not None:
                labels = self.accelerator.gather_for_metrics((labels))
                labels_host = labels if labels_host is None else nested_concat(labels_host, labels, padding_index=-100)

            self.control = self.callback_handler.on_prediction_step(args, self.state, self.control)

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if (
                args.eval_accumulation_steps is not None
                and (step + 1) % args.eval_accumulation_steps == 0
                and (self.accelerator.sync_gradients or version.parse(accelerate_version) > version.parse("0.20.3"))
            ):
                if losses_host is not None:
                    losses = nested_numpify(losses_host)
                    all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
                if preds_host is not None:
                    logits = nested_numpify(preds_host)
                    all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
                if inputs_host is not None:
                    inputs_decode = nested_numpify(inputs_host)
                    all_inputs = (
                        inputs_decode
                        if all_inputs is None
                        else nested_concat(all_inputs, inputs_decode, padding_index=-100)
                    )
                if labels_host is not None:
                    labels = nested_numpify(labels_host)
                    all_labels = (
                        labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)
                    )

                # Set back to None to begin a new accumulation
                losses_host, preds_host, inputs_host, labels_host = None, None, None, None

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Gather all remaining tensors and put them back on the CPU
        if losses_host is not None:
            losses = nested_numpify(losses_host)
            all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
        if preds_host is not None:
            logits = nested_numpify(preds_host)
            all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
        if inputs_host is not None:
            inputs_decode = nested_numpify(inputs_host)
            all_inputs = (
                inputs_decode if all_inputs is None else nested_concat(all_inputs, inputs_decode, padding_index=-100)
            )
        if labels_host is not None:
            labels = nested_numpify(labels_host)
            all_labels = labels if all_labels is None else nested_concat(all_labels, labels, padding_index=-100)

        # Number of samples
        if has_length(eval_dataset):
            num_samples = len(eval_dataset)
        # The instance check is weird and does not actually check for the type, but whether the dataset has the right
        # methods. Therefore we need to make sure it also has the attribute.
        elif isinstance(eval_dataset, IterableDatasetShard) and getattr(eval_dataset, "num_examples", 0) > 0:
            num_samples = eval_dataset.num_examples
        else:
            if has_length(dataloader):
                num_samples = self.num_examples(dataloader)
            else:  # both len(dataloader.dataset) and len(dataloader) fail
                num_samples = observed_num_examples
        if num_samples == 0 and observed_num_examples > 0:
            num_samples = observed_num_examples

        # Metrics!
        if self.compute_metrics is not None and all_preds is not None and all_labels is not None:
            if args.include_inputs_for_metrics:
                metrics = self.compute_metrics(
                    EvalPrediction(predictions=all_preds, label_ids=all_labels, inputs=all_inputs)
                )
            else:
                metrics = self.compute_metrics(EvalPrediction(predictions=all_preds, label_ids=all_labels))
        else:
            metrics = {}

        # To be JSON-serializable, we need to remove numpy types or zero-d tensors
        metrics = denumpify_detensorize(metrics)

        if all_losses is not None:
            metrics[f"{metric_key_prefix}_loss"] = all_losses.mean().item()
        if hasattr(self, "jit_compilation_time"):
            metrics[f"{metric_key_prefix}_jit_compilation_time"] = self.jit_compilation_time

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return EvalLoopOutput(predictions=all_preds, label_ids=all_labels, metrics=metrics, num_samples=num_samples)

    def train(
        self,
        resume_from_checkpoint: Optional[Union[str, bool]] = None,
        trial=None,  # No type-annotation for this one because it is related to the optuna package.
        ignore_keys_for_eval: Optional[List[str]] = None,
        **kwargs,
    ):
        with hub_neuronx_cache("training", entry=self.model_cache_entry):
            result = super().train(
                resume_from_checkpoint=resume_from_checkpoint,
                trial=trial,
                ignore_keys_for_eval=ignore_keys_for_eval,
                **kwargs,
            )
        if not is_precompilation():
            self.synchronize_hub_cache()
        return result

    def evaluate(
        self,
        eval_dataset: Optional[Union[Dataset, Dict[str, Dataset]]] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> Dict[str, float]:
        with hub_neuronx_cache("training", entry=self.model_cache_entry):
            with self.args.world_size_as_dp_size():
                result = super().evaluate(
                    eval_dataset=eval_dataset, ignore_keys=ignore_keys, metric_key_prefix=metric_key_prefix
                )
        if not is_precompilation():
            self.synchronize_hub_cache()
        return result

    def predict(
        self, test_dataset: Dataset, ignore_keys: Optional[List[str]] = None, metric_key_prefix: str = "test"
    ) -> PredictionOutput:
        with hub_neuronx_cache("training", entry=self.model_cache_entry):
            with self.args.world_size_as_dp_size():
                result = super().predict(test_dataset, ignore_keys=ignore_keys, metric_key_prefix=metric_key_prefix)
        if not is_precompilation():
            self.synchronize_hub_cache()
        return result

    @patch_within_function(("transformers.Trainer.is_world_process_zero", is_main_worker_for_metrics_method))
    def save_metrics(self, split, metrics, combined=True):
        return super().save_metrics(split, metrics, combined=combined)

    @patch_within_function(("transformers.Trainer.is_world_process_zero", is_main_worker_for_metrics_method))
    def save_state(self):
        return super().save_state()


class NeuronTrainer(AugmentTrainerForNeuronMixin, Trainer):
    """
    Trainer that is suited for performing training on AWS Tranium instances.
    """


class Seq2SeqNeuronTrainer(AugmentTrainerForNeuronMixin, Seq2SeqTrainer):
    """
    Seq2SeqTrainer that is suited for performing training on AWS Tranium instances.
    """
