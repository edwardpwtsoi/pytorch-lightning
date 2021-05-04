# Copyright The PyTorch Lightning team.
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
import contextlib
from typing import Any, Dict, Generator, List, Optional, Union

import torch

from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning.plugins.environments.cluster_environment import ClusterEnvironment
from pytorch_lightning.plugins.training_type.ddp import DDPPlugin
from pytorch_lightning.utilities import _FAIRSCALE_FULLY_SHARDED_AVAILABLE
from pytorch_lightning.utilities.exceptions import MisconfigurationException

if _FAIRSCALE_FULLY_SHARDED_AVAILABLE:
    from fairscale.nn import auto_wrap, default_auto_wrap_policy, enable_wrap, FlattenParamsWrapper, wrap
    from fairscale.nn.data_parallel import FullyShardedDataParallel

    from pytorch_lightning.overrides.fairscale import LightningFullyShardedModule, unwrap_lightning_module_fully_sharded


class FullyShardedPlugin(DDPPlugin):

    def __init__(
        self,
        cpu_offload: bool = False,
        flatten_parameters: bool = True,
        reshard_after_forward: bool = True,
        move_grads_to_cpu: Optional[bool] = None,
        fp32_reduce_scatter: Optional[bool] = None,
        compute_dtype: Optional[torch.dtype] = None,
        bucket_cap_mb: int = 25,
        module_wrap: bool = False,
        module_auto_wrap: bool = False,
        min_num_params: int = 1e8,
        parallel_devices: Optional[List[torch.device]] = None,
        num_nodes: Optional[int] = None,
        cluster_environment: ClusterEnvironment = None,
        sync_batchnorm: Optional[bool] = None,
    ):
        """

        Provides capabilities to run training using the Full Sharded capabilities provided by FairScale.

        Full Sharded Training shards the entire model across all available GPUs, allowing you to scale model
        size, whilst using efficient communication to reduce overhead. In practice, this means we can remain
        at parity with PyTorch DDP, whilst scaling our model sizes dramatically. The technique is similar
        to ZeRO-Stage 3 but has been built for upstreaming to PyTorch.

        `For more information: https://fairscale.readthedocs.io/en/latest/api/nn/fsdp.html`.

        .. warning:: ``FullyShardedPlugin`` is in beta and subject to change.

        Defaults have been set and options have been exposed, but may require configuration
        based on your level of memory/speed efficiency.
        We suggest having a look at this PR for more information.
        `https://github.com/facebookresearch/fairscale/pull/413`


        Many of the helpful doc strings below came from the original FairScale documentation:
        `https://fairscale.readthedocs.io/en/latest/api/nn/fsdp.html`

        Arguments:

           cpu_offload: Offload FP32 params to CPU. Only usable in precision=16 mode

           move_grads_to_cpu: Moves gradient shards to CPU after reduction.
                        Only disable if using CPU based optimizers (defaults to ``cpu_offload``).

           flatten_parameters: Flattens parameter into single contiguous tensor for speed efficiency

           reshard_after_forward: Reshard parameters after the forward pass, which saves memory but slows
                down training. Only revelant when nesting FullyShardedDataParallel wrappers inside the model

           fp32_reduce_scatter: Reduce-Scatter gradients in FP32. Only relevant in mixed precision

           compute_dtype: dtype for full parameters for computation. Default to torch.float32,
                unless using mixed precision, in which case defaults to torch.float16

           bucket_cap_mb: bucket parameters so that gradient reduction
               can potentially overlap with backward computation.
               bucket_cap_mb controls the bucket size in MegaBytes (MB).
               Buckets are sub-divided based on world_size,
               so the max shard size is roughly bucket_cap_mb / world_size.
               Values <= 0 disable bucketing

            min_num_params: Number of parameters to wrap when using FairScale ``auto_wrap``

            module_wrap: Wrap the ``LightningModule`` in a ``FullyShardedDataParallel`` wrapper

            module_auto_wrap: Automatically wrap the ``LightningModule`` with Fully Sharded recursively.
                Using ``min_num_params`` to determine the amount of parameters to wrap at a time

        """
        if not _FAIRSCALE_FULLY_SHARDED_AVAILABLE:
            raise MisconfigurationException(
                "Full Sharded Training is not available. Install the latest FairScale via `pip install fairscale -U`"
            )

        if module_wrap or module_auto_wrap:
            raise MisconfigurationException(
                "Currently wrapping the ``LightningModule`` in the plugin is not supported. "
                "Please wrap your model manually in the ``configure_sharded_model`` function"
            )
        super().__init__(parallel_devices, num_nodes, cluster_environment, sync_batchnorm)
        self.cpu_offload = cpu_offload
        self.move_grads_to_cpu = move_grads_to_cpu
        self.flatten_parameters = flatten_parameters
        self.reshard_after_forward = reshard_after_forward
        self.fp32_reduce_scatter = fp32_reduce_scatter
        self.compute_dtype = compute_dtype
        self.bucket_cap_mb = bucket_cap_mb
        self.module_wrap = module_wrap
        self.module_auto_wrap = module_auto_wrap
        self.min_num_params = min_num_params
        self._process_group = None

    @property
    def process_group(self):
        if self._process_group is None:
            self._process_group = torch.distributed.new_group()
        return self._process_group

    def setup_distributed(self):
        super().setup_distributed()
        if self.root_device.type == "cuda":
            torch.cuda.set_device(self.root_device)

    @contextlib.contextmanager
    def model_sharded_context(self) -> Generator:
        precision = self.lightning_module.trainer.precision

        def wrap_policy(*args, **kwargs):
            return default_auto_wrap_policy(*args, **kwargs, min_num_params=self.min_num_params)

        with enable_wrap(
            wrapper_cls=FullyShardedDataParallel,
            auto_wrap_policy=wrap_policy,
            process_group=self.process_group,
            cpu_offload=self.cpu_offload,
            move_grads_to_cpu=self.move_grads_to_cpu,
            flatten_parameters=self.flatten_parameters,
            mixed_precision=precision == "mixed",
            reshard_after_forward=self.reshard_after_forward,
            fp32_reduce_scatter=self.fp32_reduce_scatter,
            compute_dtype=self.compute_dtype,
            bucket_cap_mb=self.bucket_cap_mb,
        ):
            yield

    def configure_ddp(self):
        with self.model_sharded_context():
            if self.module_auto_wrap and not self._model_has_nested_fsdp():
                self.model = auto_wrap(LightningFullyShardedModule(self.model))
                if not isinstance(self.model, FullyShardedDataParallel):
                    self.model = wrap(self.model)
            elif self.module_wrap:
                self.model = wrap(LightningFullyShardedModule(self.model))

        if not self.cpu_offload:
            # When using CPU Offload, FSDP will manage the CUDA movement for us
            self.model_to_device()
        # setup optimizers after fully sharded has wrapped the lightning module
        self.lightning_module.trainer.accelerator.setup_optimizers(self.lightning_module.trainer)

    def model_to_device(self):
        self.model.to(self.root_device)
        # ensure we update the device type in the lightning module
        self.lightning_module.to(self.root_device)

    def pre_dispatch(self):
        if self.sync_batchnorm:
            self.model = self.configure_sync_batchnorm(self.model)
        self.configure_ddp()
        self.barrier()

    @property
    def lightning_module(self) -> LightningModule:
        return unwrap_lightning_module_fully_sharded(self.model)

    def on_save(self, checkpoint: Dict[str, Union[Any, torch.Tensor]]) -> Dict[str, Union[Any, torch.Tensor]]:
        state_dict = self.collate_state_dict()
        checkpoint['state_dict'] = state_dict
        return checkpoint

    def collate_state_dict(self):
        """
        Collects the models sharded state dict from all processes before returning.
        Returns: The unsharded model state dict.
        """
        state_dict = self.model.state_dict()
        import pdb
        pdb.set_trace()
        if self.module_wrapped:
            # Remove module prefix from state dict, as we've wrapped the lightning module
            # inside an FSDP module.
            state_dict = {k.partition('module.')[2]: state_dict[k] for k in state_dict.keys()}
        return state_dict

    @property
    def setup_optimizers_in_pre_dispatch(self) -> bool:
        # Setup optimizers after the Fully Sharded Model has been made
        return True

    def _model_has_nested_fsdp(self):
        for module in self.model.modules():
            if isinstance(module, FullyShardedDataParallel):
                return True
        return False

    @classmethod
    def register_plugins(cls, plugin_registry: Dict):
        plugin_registry.register("fsdp", cls, description="Fully Sharded with LightningModule wrap", module_wrap=True)
        plugin_registry.register(
            "fsdp_auto_wrap",
            cls,
            description="Fully Sharded Training with recursive wrapping of the module.",
            module_auto_wrap=True
        )
        plugin_registry.register(
            "fsdp_manual", cls, description="Fully Sharded Training with manual wrapping of the model"
        )

    def training_step(self, *args, **kwargs):
        if self.module_wrapped:
            return super().training_step(*args, **kwargs)
        return self.model.training_step(*args, **kwargs)

    def validation_step(self, *args, **kwargs):
        if self.module_wrapped:
            return super().validation_step(*args, **kwargs)
        return self.model.validation_step(*args, **kwargs)

    def test_step(self, *args, **kwargs):
        if self.module_wrapped:
            return super().test_step(*args, **kwargs)
        return self.model.test_step(*args, **kwargs)

    def predict_step(self, *args, **kwargs):
        if self.module_wrapped:
            return super().predict_step(*args, **kwargs)
        return self.model.predict_step(*args, **kwargs)

    def post_training_step(self):
        pass

    @property
    def module_wrapped(self) -> bool:
        return self.module_wrap or self.module_auto_wrap