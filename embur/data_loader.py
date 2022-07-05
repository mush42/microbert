from typing import Any, Dict, Iterable, Iterator, Union, Optional
import itertools
import math

import torch


from allennlp.common import util
from allennlp.data.batch import Batch
from allennlp.data.data_loaders.data_loader import DataLoader, TensorDict
from allennlp.data.data_loaders.multiprocess_data_loader import MultiProcessDataLoader
from allennlp.data.data_loaders.multitask_scheduler import MultiTaskScheduler
from allennlp.data.data_loaders.multitask_epoch_sampler import MultiTaskEpochSampler
from allennlp.data.dataset_readers.multitask import MultiTaskDatasetReader
from allennlp.data.instance import Instance
from allennlp.data.vocabulary import Vocabulary
import allennlp.nn.util as nn_util


def maybe_shuffle_instances(loader: DataLoader, shuffle: bool) -> Iterable[Instance]:
    if shuffle:
        return util.shuffle_iterable(loader.iter_instances())
    else:
        return loader.iter_instances()


@DataLoader.register("multitask_ldg")
class MultiTaskDataLoaderLdg(DataLoader):
    """
    A `DataLoader` intended for multi-task learning.  The basic idea is that you use a
    `MultiTaskDatasetReader`, which takes a dictionary of `DatasetReaders`, keyed by some name.  You
    use those same names for various parameters here, including the data paths that get passed to
    each reader.  We will load each dataset and iterate over instances in them using a
    `MultiTaskEpochSampler` and a `MultiTaskScheduler`.  The `EpochSampler` says how much to use
    from each dataset at each epoch, and the `Scheduler` orders the instances in the epoch however
    you want.  Both of these are designed to be used in conjunction with trainer `Callbacks`, if
    desired, to have the sampling and/or scheduling behavior be dependent on the current state of
    training.

    While it is not necessarily required, this `DatasetReader` was designed to be used alongside a
    `MultiTaskModel`, which can handle instances coming from different datasets.  If your datasets
    are similar enough (say, they are all reading comprehension datasets with the same format), or
    your model is flexible enough, then you could feasibly use this `DataLoader` with a normal,
    non-multitask `Model`.

    Registered as a `DataLoader` with name "multitask".

    # Parameters

    reader: `MultiTaskDatasetReader`
    data_path: `Dict[str, str]`
        One file per underlying dataset reader in the `MultiTaskDatasetReader`, which will be passed
        to those readers to construct one `DataLoader` per dataset.
    scheduler: `MultiTaskScheduler`, optional (default = `HomogeneousRoundRobinScheduler`)
        The `scheduler` determines how instances are ordered within an epoch.  By default, we'll
        select one batch of instances from each dataset in turn, trying to ensure as uniform a mix
        of datasets as possible.  Note that if your model can handle it, using a
        `RoundRobinScheduler` is likely better than a `HomogeneousRoundRobinScheduler` (because it
        does a better job mixing gradient signals from various datasets), so you may want to
        consider switching.  We use the homogeneous version as default because it should work for
        any allennlp model, while the non-homogeneous one might not.
    sampler: `MultiTaskEpochSampler`, optional (default = `None`)
        Only used if `instances_per_epoch` is not `None`. If we need to select a subset of the data
        for an epoch, this `sampler` will tell us with what proportion we should sample from each
        dataset.  For instance, we might want to focus more on datasets that are underperforming in
        some way, by having those datasets contribute more instances this epoch than other datasets.
    instances_per_epoch: `int`, optional (default = `None`)
        If not `None`, we will use this many instances per epoch of training, drawing from the
        underlying datasets according to the `sampler`.
    num_workers: `Dict[str, int]`, optional (default = `None`)
        Used when creating one `MultiProcessDataLoader` per dataset.  If you want non-default
        behavior for this parameter in the `DataLoader` for a particular dataset, pass the
        corresponding value here, keyed by the dataset name.
    max_instances_in_memory: `Dict[str, int]`, optional (default = `None`)
        Used when creating one `MultiProcessDataLoader` per dataset.  If you want non-default
        behavior for this parameter in the `DataLoader` for a particular dataset, pass the
        corresponding value here, keyed by the dataset name.
    start_method: `Dict[str, str]`, optional (default = `None`)
        Used when creating one `MultiProcessDataLoader` per dataset.  If you want non-default
        behavior for this parameter in the `DataLoader` for a particular dataset, pass the
        corresponding value here, keyed by the dataset name.
    instance_queue_size: `Dict[str, int]`, optional (default = `None`)
        Used when creating one `MultiProcessDataLoader` per dataset.  If you want non-default
        behavior for this parameter in the `DataLoader` for a particular dataset, pass the
        corresponding value here, keyed by the dataset name.
    instance_chunk_size: `Dict[str, int]`, optional (default = `None`)
        Used when creating one `MultiProcessDataLoader` per dataset.  If you want non-default
        behavior for this parameter in the `DataLoader` for a particular dataset, pass the
        corresponding value here, keyed by the dataset name.
    shuffle: `bool`, optional (default = `True`)
        If `False`, we will not shuffle the instances that come from each underlying data loader.
        You almost certainly never want to use this except when debugging.
    cuda_device: `Optional[Union[int, str, torch.device]]`, optional (default = `None`)
        If given, batches will automatically be put on this device.

        !!! Note
            This should typically not be set in an AllenNLP configuration file. The `Trainer`
            will automatically call [`set_target_device()`](#set_target_device) before iterating
            over batches.
    """

    def __init__(
        self,
        reader: MultiTaskDatasetReader,
        data_path: Dict[str, str],
        scheduler: MultiTaskScheduler,
        *,
        sampler: MultiTaskEpochSampler = None,
        instances_per_epoch: int = None,
        num_workers: Dict[str, int] = None,
        max_instances_in_memory: Dict[str, int] = None,
        start_method: Dict[str, str] = None,
        instance_queue_size: Dict[str, int] = None,
        instance_chunk_size: Dict[str, int] = None,
        shuffle: bool = True,
        cuda_device: Optional[Union[int, str, torch.device]] = None,
    ) -> None:
        is_validation = "/dev" in data_path["mlm"]
        print(
            f"Data path is {data_path['mlm']} and I think it",
            "IS" if is_validation else "is NOT",
            "a validation path. If this is wrong, revise embur.data_loader, line 118",
        )
        if is_validation:
            instances_per_epoch = None

        self.readers = reader.readers
        self.data_paths = data_path
        self.scheduler = scheduler
        self.sampler = sampler
        self.cuda_device: Optional[torch.device] = None
        if cuda_device is not None:
            if not isinstance(cuda_device, torch.device):
                self.cuda_device = torch.device(cuda_device)
            else:
                self.cuda_device = cuda_device

        self._instances_per_epoch = instances_per_epoch
        self._shuffle = shuffle

        if instances_per_epoch is not None and sampler is None:
            raise ValueError("You must provide an EpochSampler if you want to not use all instances every epoch.")

        self._num_workers = num_workers or {}
        self._max_instances_in_memory = max_instances_in_memory or {}
        self._start_method = start_method or {}
        self._instance_queue_size = instance_queue_size or {}
        self._instance_chunk_size = instance_chunk_size or {}

        if self.readers.keys() != self.data_paths.keys():
            raise ValueError(
                f"Mismatch between readers ({self.readers.keys()}) and data paths " f"({self.data_paths.keys()})"
            )
        self._loaders = {key: self._make_data_loader(key) for key in self.readers}

        # This stores our current iterator with each dataset, so we don't just iterate over the
        # first k instances every epoch if we're using instances_per_epoch.  We'll grab instances
        # from here each epoch, and refresh it when it runs out.  We only use this in the case that
        # instances_per_epoch is not None, but these iterators are lazy, so always creating them
        # doesn't hurt anything.
        self._iterators: Dict[str, Iterator[Instance]] = {
            # NOTE: The order in which we're calling these iterator functions is important.  We want
            # an infinite iterator over the data, but we want the order in which we iterate over the
            # data to be different at every epoch.  The cycle function will give us an infinite
            # iterator, and it will call the lambda function each time it runs out of instances,
            # which will produce a new shuffling of the dataset.
            key: util.cycle_iterator_function(
                # This default argument to the lambda function is necessary to create a new scope
                # for the loader variable, so a _different_ loader gets saved for every iterator.
                # Dictionary comprehensions don't create new scopes in python.  If you don't have
                # this loader, you end up with `loader` always referring to the last loader in the
                # iteration... mypy also doesn't know what to do with this, for some reason I can't
                # figure out.
                lambda l=loader: maybe_shuffle_instances(l, self._shuffle)  # type: ignore
            )
            for key, loader in self._loaders.items()
        }

    def __len__(self) -> int:
        if self._instances_per_epoch is None:
            # This will raise a TypeError if any of the underlying loaders doesn't have a length,
            # which is actually what we want.
            return self.scheduler.count_batches({dataset: len(loader) for dataset, loader in self._loaders.items()})
        else:
            return self.scheduler.count_batches(
                {dataset: self._instances_per_epoch for dataset in self._loaders.keys()}
            )

    def __iter__(self) -> Iterator[TensorDict]:
        epoch_instances = self._get_instances_for_epoch()
        return (
            nn_util.move_to_device(
                Batch(instances).as_tensor_dict(),
                -1 if self.cuda_device is None else self.cuda_device,
            )
            for instances in self.scheduler.batch_instances(epoch_instances)
        )

    def iter_instances(self) -> Iterator[Instance]:
        # The only external contract for this method is that it iterates over instances
        # individually; it doesn't actually specify anything about batching or anything else.  The
        # implication is that you iterate over all instances in the dataset, in an arbitrary order.
        # The only external uses of this method are in vocabulary construction (the
        # MultiProcessDataLoader uses this function internally when constructing batches, but that's
        # an implementation detail).
        #
        # So, the only thing we need to do here is iterate over all instances from all datasets, and
        # that's sufficient.  We won't be using this for batching, because that requires some
        # complex, configurable scheduling.
        #
        # The underlying data loaders here could be using multiprocessing; we don't need to worry
        # about that in this class. Caching is also handled by the underlying data loaders.
        for loader in self._loaders.values():
            yield from loader.iter_instances()

    def index_with(self, vocab: Vocabulary) -> None:
        for loader in self._loaders.values():
            loader.index_with(vocab)

    def set_target_device(self, device: torch.device) -> None:
        self.cuda_device = device

    def _get_instances_for_epoch(self) -> Dict[str, Iterable[Instance]]:
        if self._instances_per_epoch is None:
            return {key: maybe_shuffle_instances(loader, self._shuffle) for key, loader in self._loaders.items()}
        if self.sampler is None:
            # We already checked for this in the constructor, so this should never happen unless you
            # modified the object after creation. But mypy is complaining, so here's another check.
            raise ValueError("You must specify an EpochSampler if self._instances_per_epoch is not None.")
        dataset_proportions = self.sampler.get_task_proportions(self._loaders)
        proportion_sum = sum(dataset_proportions.values())
        num_instances_per_dataset = {
            key: math.floor(proportion * self._instances_per_epoch / proportion_sum)
            for key, proportion in dataset_proportions.items()
        }
        return {
            key: itertools.islice(self._iterators[key], num_instances)
            for key, num_instances in num_instances_per_dataset.items()
        }

    def _make_data_loader(self, key: str) -> MultiProcessDataLoader:
        kwargs: Dict[str, Any] = {
            "reader": self.readers[key],
            "data_path": self.data_paths[key],
            # We don't load batches from this data loader, only instances, but we have to set
            # something for the batch size, so we set 1.
            "batch_size": 1,
        }
        if key in self._num_workers:
            kwargs["num_workers"] = self._num_workers[key]
        if key in self._max_instances_in_memory:
            kwargs["max_instances_in_memory"] = self._max_instances_in_memory[key]
        if key in self._start_method:
            kwargs["start_method"] = self._start_method[key]
        return MultiProcessDataLoader(**kwargs)


from collections import deque
import logging
from multiprocessing.process import BaseProcess
from multiprocessing.connection import Connection
import random
import traceback
import select
from queue import Full
from typing import List, Iterator, Optional, Iterable, Union, TypeVar, Tuple, Any


import torch
import torch.multiprocessing as mp

from allennlp.common.util import lazy_groups_of, shuffle_iterable
from allennlp.common.tqdm import Tqdm
from allennlp.data.instance import Instance
from allennlp.data.data_loaders.data_loader import DataLoader, TensorDict
from allennlp.data.data_loaders.data_collator import DataCollator, DefaultDataCollator
from allennlp.data.dataset_readers import DatasetReader, WorkerInfo, DatasetReaderInput
from allennlp.data.fields import TextField
from allennlp.data.samplers import BatchSampler
from allennlp.data.vocabulary import Vocabulary
import allennlp.nn.util as nn_util


logger = logging.getLogger(__name__)


_T = TypeVar("_T")


@DataLoader.register("multiprocess_ldg")
class MultiProcessDataLoaderLdg(DataLoader):
    """
    The `MultiProcessDataLoader` is a [`DataLoader`](../data_loader/#dataloader)
    that's optimized for AllenNLP experiments.

    See
    [Using your reader with multi-process or distributed data loading](/api/data/dataset_readers/dataset_reader/#datasetreader.using_your_reader_with_multi-process_or_distributed_data_loading)
    for more information on how to optimize your `DatasetReader` for use with this `DataLoader`.

    # Parameters

    reader: `DatasetReader`, required
        A `DatasetReader` used to load instances from the `data_path`.

    data_path: `DatasetReaderInput`, required
        Passed to `DatasetReader.read()`.

        !!! Note
            In a typical AllenNLP configuration file, the `reader` and `data_path` parameters don't
            get an entry under the `data_loader`. The `reader` is constructed separately from
            the corresponding `dataset_reader` params, and the `data_path` is taken from the
            `train_data_path`, `validation_data_path`, or `test_data_path`.

    batch_size: `int`, optional (default = `None`)
        When `batch_sampler` is unspecified, this option can be combined with `drop_last`
        and `shuffle` to control automatic batch sampling.

    drop_last: `bool`, optional (default = `False`)
        When `batch_sampler` is unspecified, this option can be combined with `batch_size`
        and `shuffle` to control automatic batch sampling.

        If `True`, the last batch will be dropped if it doesn't contain a full `batch_size`
        number of `Instance`s.

    shuffle: `bool`, optional (default = `False`)
        When `batch_sampler` is unspecified, this option can be combined with `batch_size`
        and `drop_last` to control automatic batch sampling.

    batch_sampler: `BatchSampler`, optional (default = `None`)
        A `BatchSampler` to handle batching. This option is mutually exclusive with
        `batch_size`, `drop_last`, and `shuffle`.

    batches_per_epoch: `int`, optional (default = `None`)
        If specified, exactly `batches_per_epoch` batches will be generated with each call
        to `__iter__()`.

    num_workers: `int`, optional (default = `0`)
        The number of workers to use to read `Instances` in parallel.
        If `num_workers = 0`, everything is done in the main process. Otherwise `num_workers`
        workers are forked or spawned (depending on the value of `start_method`), each of which
        calls `read()` on their copy of the `reader`.

        This means that in order for multi-process loading to be efficient when `num_workers > 1`,
        the `reader` needs to implement
        [`manual_multiprocess_sharding`](/api/data/dataset_readers/dataset_reader/#datasetreader).

        !!! Warning
            Multi-processing code in Python is complicated! We highly recommend you read the short
            [Best practices](#multiprocessdataloader.best_practices) and
            [Common issues](#multiprocessdataloader.common_issues) sections below before using this option.

    max_instances_in_memory: `int`, optional (default = `None`)
        If not specified, all instances will be read and cached in memory for the duration
        of the data loader's life. This is generally ideal when your data can fit in memory
        during training. However, when your datasets are too big, using this option
        will turn on lazy loading, where only `max_instances_in_memory` instances are processed
        at a time.

        !!! Note
            This setting will affect how a `batch_sampler` is applied. If
            `max_instances_in_memory` is `None`, the sampler will be applied to all `Instances`.
            Otherwise the sampler will be applied to only `max_instances_in_memory` `Instances`
            at a time.

            Therefore when using this option with a sampler, you should generally set it to a multiple of
            the sampler's `batch_size` (if it has one).

    start_method: `str`, optional (default = `"fork"`)
        The [start method](https://docs.python.org/3.7/library/multiprocessing.html#contexts-and-start-methods)
        used to spin up workers.

        On Linux or OS X, "fork" usually has the lowest overhead for starting workers
        but could potentially lead to dead-locks if you're using lower-level libraries that are not fork-safe.

        If you run into these issues, try using "spawn" instead.

    cuda_device: `Optional[Union[int, str, torch.device]]`, optional (default = `None`)
        If given, batches will automatically be put on this device.

        !!! Note
            This should typically not be set in an AllenNLP configuration file. The `Trainer`
            will automatically call [`set_target_device()`](#set_target_device) before iterating
            over batches.

    quiet : `bool`, optional (default = `False`)
        If `True`, tqdm progress bars will be disabled.

    collate_fn : `DataCollator`, optional ( default = `DefaultDataCollator`)

    # Best practices

    - **Large datasets**

        If your dataset is too big to fit into memory (a common problem), you'll need to load it lazily.
        This is done by simply setting the `max_instances_in_memory` parameter to a non-zero integer.
        The optimal value depends on your use case.

        If you're using a `batch_sampler`, you will generally get better samples by setting
        `max_instances_in_memory` to a higher number - such as 10 to 100 times your batch size -
        since this determines how many `Instances` your `batch_sampler` gets to sample from at a time.

        If you're not using a `batch_sampler` then this number is much less important. Setting it to
        2 to 10 times your batch size is a reasonable value.

        Keep in mind that using `max_instances_in_memory` generally results in a slower
        training loop unless you load data in worker processes by setting the `num_workers` option to a
        non-zero integer (see below). That way data loading won't block the main process.

    - **Performance**

        The quickest way to increase the performance of data loading is adjust the `num_workers` parameter.
        `num_workers` determines how many workers are used to read `Instances` from your
        `DatasetReader`. By default, this is set to `0`, which means everything is done in the main process.

        Before trying to set `num_workers` to a non-zero number, you should make sure your `DatasetReader`
        is [optimized for use with multi-process data loading]
        (/api/data/dataset_readers/dataset_reader/#datasetreader.using_your_reader_with_multi-process_or_distributed_data_loading).

    # Common issues

    - **Dead-locks**

        Multiprocessing code in Python is complicated! Especially code that involves lower-level libraries
        which may be spawning their own threads. If you run into dead-locks while
        using `num_workers > 0`, luckily there are two simple work-arounds which usually fix the issue.

        The first work-around is to disable parallelism for these low-level libraries.
        For example, setting the environment variables `OMP_NUM_THREADS=1` and `TOKENIZERS_PARALLELISM=0`
        will do so for PyTorch and Numpy (for CPU operations) and HuggingFace Tokenizers, respectively.

        Alternatively, changing the `start_method` to "spawn" (when available, depending on your OS)
        may fix your issues without disabling parallelism for other libraries.

        See [issue #4848](https://github.com/allenai/allennlp/issues/4848) for more info.

        Dead-locks could also be caused by running out of shared memory (see below).

    - **Shared memory restrictions**

        Tensors are passed between processes using shared memory, and some systems impose strict
        limits on the allowed size of shared memory.

        Luckily this is simple to debug and simple to fix.

        First, to verify that this is your issue just watch your shared memory as your data loader runs.
        For example, run `watch -n 0.3 'df -h | grep shm'`.

        If you're seeing your shared memory blow up until it maxes-out, then you either need to decrease
        `max_instances_in_memory` or increase your system's `ulimit`.

        If you're using Docker, you can increase the shared memory available on a container by running
        it with the option `--ipc=host` or by setting `--shm-size`.

        See [issue #4847](https://github.com/allenai/allennlp/issues/4847) for more info.

    """  # noqa: E501

    def __init__(
        self,
        reader: DatasetReader,
        data_path: DatasetReaderInput,
        *,
        batch_size: int = None,
        drop_last: bool = False,
        shuffle: bool = False,
        batch_sampler: BatchSampler = None,
        batches_per_epoch: int = None,
        num_workers: int = 0,
        max_instances_in_memory: int = None,
        start_method: str = "fork",
        cuda_device: Optional[Union[int, str, torch.device]] = None,
        quiet: bool = False,
        collate_fn: DataCollator = DefaultDataCollator(),
    ) -> None:
        is_validation = "dev" in data_path
        print(
            f"Data path is {data_path} and I think it",
            "IS" if is_validation else "is NOT",
            "a validation path. If this is wrong, revise embur.data_loader, line 118",
        )
        if is_validation:
            batches_per_epoch = None

        # Do some parameter validation.
        if num_workers is not None and num_workers < 0:
            raise ValueError("num_workers cannot be a negative number")

        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        if batch_sampler is not None:
            if batch_size is not None:
                raise ValueError("batch_sampler option is mutually exclusive with batch_size")

            if drop_last:
                raise ValueError("batch_sampler option is mutually exclusive with drop_last")

            if shuffle:
                raise ValueError("batch_sampler option is mutually exclusive with shuffle")
        elif batch_size is None:
            raise ValueError("batch_size is required when batch_sampler is not supplied")

        if batches_per_epoch is not None and batches_per_epoch < 1:
            raise ValueError("batches_per_epoch must be at least 1")

        if max_instances_in_memory is not None:
            if batch_size is not None and max_instances_in_memory < batch_size:
                raise ValueError("max_instances_in_memory must be at least batch_size")
            elif max_instances_in_memory < 1:
                raise ValueError("max_instances_in_memory must be at least 1")

        self.reader = reader
        self.data_path = data_path
        self.batch_size = batch_size
        self.drop_last = drop_last
        self.shuffle = shuffle
        self.batch_sampler = batch_sampler
        self.batches_per_epoch = batches_per_epoch
        self.num_workers = num_workers
        self.collate_fn = collate_fn
        self.max_instances_in_memory = max_instances_in_memory
        self.start_method = start_method
        self.quiet = quiet
        self.cuda_device: Optional[torch.device] = None
        if cuda_device is not None:
            if not isinstance(cuda_device, torch.device):
                self.cuda_device = torch.device(cuda_device)
            else:
                self.cuda_device = cuda_device

        # Can only initialize CUDA in workers when these `start_methods` are used.
        self._worker_cuda_safe = self.start_method in {"spawn", "forkserver"}

        # To make sure we have some backpressure in the worker queues we try to set
        # reasonable defaults for the maximum size of these queues.
        # They have to be big enough that is doesn't hurt performance, but small enough
        # that they don't take up too many resources when there is a bottleneck on the
        # consuming end of a queue.
        effective_batch_size = self.batch_size if self.batch_sampler is None else self.batch_sampler.get_batch_size()
        self._max_instance_queue_size = (
            None if max_instances_in_memory is None else 2 * self.num_workers * max_instances_in_memory
        )
        self._max_batch_queue_size = (
            None
            if max_instances_in_memory is None
            else 2 * self.num_workers * max_instances_in_memory // (effective_batch_size or 1)
        )

        # If max_instances_in_memory is not given, we'll keep a cache of all instances in this list.
        self._instances: Optional[List[Instance]] = None
        # Keeps track of state when `batches_per_epoch` is used.
        self._batch_generator: Optional[Iterator[TensorDict]] = None
        # For indexing instances.
        self._vocab: Optional[Vocabulary] = None

        if self.max_instances_in_memory is None:
            # Load all instances right away.
            deque(self.iter_instances(), maxlen=0)

    def index_with(self, vocab: Vocabulary) -> None:
        self._vocab = vocab
        if self._instances:
            for instance in self._instances:
                instance.index_fields(vocab)

    def __len__(self) -> int:
        if self.batches_per_epoch is not None:
            return self.batches_per_epoch
        elif self.max_instances_in_memory is None:
            # We haven't read the instances yet, so we do so now, caching them as we go.
            if not self._instances:
                deque(self.iter_instances(), maxlen=0)

            if self.batch_sampler is not None:
                return self.batch_sampler.get_num_batches(self._instances)  # type: ignore

            num_instances = len(self._instances)  # type: ignore
            # We know batch_size won't be None here since `batch_sampler` is None.
            batch_size: int = self.batch_size  # type: ignore
            if self.drop_last or num_instances % batch_size == 0:
                return num_instances // batch_size
            else:
                return 1 + num_instances // batch_size
        else:
            # We can't know the number of batches for a lazy loader when batches_per_epoch
            # is not specified.
            raise TypeError

    def __iter__(self) -> Iterator[TensorDict]:
        if self._vocab is None:
            raise ValueError(
                "This DataLoader has not been indexed with a Vocabulary yet. "
                "Did you forget to call DataLoader.index_with(vocab)?"
            )

        if self.batches_per_epoch is None:
            yield from self._iter_batches()
        else:
            if self._batch_generator is not None:
                batch_generator = self._batch_generator
                # Can't have a pointer to this in `self` when we try to spawn workers.
                self._batch_generator = None
            else:
                batch_generator = self._iter_batches()
            for i in range(self.batches_per_epoch):
                try:
                    yield next(batch_generator)
                except StopIteration:  # batch_generator is exhausted
                    batch_generator = self._iter_batches()  # so refresh it
                    yield next(batch_generator)
            self._batch_generator = batch_generator

    def iter_instances(self) -> Iterator[Instance]:
        if self._instances:
            yield from self._instances
        else:
            if self.max_instances_in_memory is None:
                self._instances = []

            if self.num_workers <= 0:
                # Just read all instances in main process.
                for instance in self._maybe_tqdm(self.reader.read(self.data_path), desc="loading instances"):
                    self.reader.apply_token_indexers(instance)
                    if self.max_instances_in_memory is None:
                        self._instances.append(instance)  # type: ignore
                    if self._vocab is not None:
                        instance.index_fields(self._vocab)
                    yield instance
            else:
                ctx = mp.get_context(self.start_method)
                queue: mp.JoinableQueue = (
                    ctx.JoinableQueue()
                    if self._max_instance_queue_size is None
                    else ctx.JoinableQueue(maxsize=self._max_instance_queue_size)
                )
                workers, txs = self._start_instance_workers(queue, ctx)

                try:
                    for instance in self._maybe_tqdm(self._gather_instances(queue), desc="loading instances"):
                        if self.max_instances_in_memory is None:
                            self._instances.append(instance)  # type: ignore
                        yield instance
                finally:
                    if hasattr(queue, "close"):  # for compat with different Python versions.
                        queue.close()  # type: ignore[attr-defined]
                    self._join_workers(workers, queue, txs)

    def set_target_device(self, device: torch.device) -> None:
        self.cuda_device = device

    def _iter_batches(self) -> Iterator[TensorDict]:
        if self._instances is not None or self.num_workers <= 0:
            for batch in self._instances_to_batches(self.iter_instances(), move_to_device=True):
                yield batch
        else:
            ctx = mp.get_context(self.start_method)

            queue: mp.JoinableQueue = (
                ctx.JoinableQueue()
                if self._max_batch_queue_size is None
                else ctx.JoinableQueue(maxsize=self._max_batch_queue_size)
            )
            workers, txs = self._start_batch_workers(queue, ctx)

            try:
                # We can now start consuming from the `queue` as the batch workers
                # produce batches.
                done_count: int = 0
                while done_count < self.num_workers:
                    for batch, worker_error in iter(queue.get, (None, None)):
                        if worker_error is not None:
                            e, tb = worker_error
                            raise WorkerError(e, tb)

                        if not self._worker_cuda_safe and self.cuda_device is not None:
                            # Need to move batch to target device now.
                            batch = nn_util.move_to_device(batch, self.cuda_device)
                        yield batch
                        queue.task_done()
                    done_count += 1
            finally:
                if hasattr(queue, "close"):  # for compat with different Python versions.
                    queue.close()  # type: ignore[attr-defined]
                self._join_workers(workers, queue, txs)

    def _start_instance_workers(self, queue: mp.JoinableQueue, ctx) -> Tuple[List[BaseProcess], List[Connection]]:
        Tqdm.set_lock(mp.RLock())
        workers: List[BaseProcess] = []
        txs: List[Connection] = []
        for worker_id in range(self.num_workers):
            rx, tx = ctx.Pipe(duplex=False)
            worker: BaseProcess = ctx.Process(
                target=self._instance_worker,
                args=(worker_id, queue, Tqdm.get_lock(), rx),
                daemon=True,
            )
            worker.start()
            workers.append(worker)
            txs.append(tx)
        return workers, txs

    def _start_batch_workers(self, queue: mp.JoinableQueue, ctx) -> Tuple[List[BaseProcess], List[Connection]]:
        Tqdm.set_lock(mp.RLock())
        workers: List[BaseProcess] = []
        txs: List[Connection] = []
        for worker_id in range(self.num_workers):
            rx, tx = ctx.Pipe(duplex=False)
            worker: BaseProcess = ctx.Process(
                target=self._batch_worker, args=(worker_id, queue, Tqdm.get_lock(), rx), daemon=True
            )
            worker.start()
            workers.append(worker)
            txs.append(tx)
        return workers, txs

    def _join_workers(self, workers: List[BaseProcess], queue, txs: List[Connection]) -> None:
        # If the workers have exhausted their batch/instance generators,
        # they will be blocking on a call to `queue.join()`,
        # so calling `queue.task_done()` times the number of workers will
        # allow the `queue.join()` to return and each worker should exit on its own.
        for _ in range(len(workers)):
            try:
                queue.task_done()
            except ValueError:
                # This happens if a worker died early.
                break
        # But if we're joining the workers due to an exception in the main process,
        # they probably won't be finished, so we need to tell them to stop.
        # We first do this nicely by sending them a message through their corresponding
        # tx connection.
        for tx in txs:
            tx.send("stop")

        # If for some reason the workers still haven't exited, we go through and terminate
        # them.
        for i, worker in enumerate(workers):
            worker.join(1)
            if worker.is_alive():
                logger.warning("terminating worker %s", i)
                worker.terminate()

    def _safe_queue_put(self, worker_id: int, item: Any, queue: mp.JoinableQueue, rx: Connection) -> bool:
        while True:
            # First we have to check to make sure the parent process is still alive
            # and consuming from the queue because there are circumstances where the
            # parent process can or exit stop consuming without automatically cleaning up
            # its children (the workers).
            # For example, when the parent process is killed with `kill -9`.
            # So the first thing we do is check to see if the parent has notified
            # us (the worker) to stop through the rx (receiver) connection.
            # Of course this only works if the parent was able to send out a notification,
            # which may not always be the case. So we have a backup check below.
            if rx.poll():
                logger.warning("worker %d received stop message from parent, exiting now", worker_id)
                queue.cancel_join_thread()
                return False
            # The is the backup check.
            # The file descriptor associated with the rx (receiver) connection will
            # be readable if and only if the parent process has exited.
            # NOTE (epwalsh): this doesn't work on Mac OS X with `start_method == "fork"`
            # for some reason, i.e. the file descriptor doesn't show as readable
            # after the parent process has died.
            fds, _, _ = select.select([rx.fileno()], [], [], 0)
            if fds:
                logger.warning("worker %d parent process has died, exiting now", worker_id)
                queue.cancel_join_thread()
                return False
            # If we're down here the parent process is still alive to the best of our
            # knowledge, so we can continue putting things on the queue.
            try:
                queue.put(item, True, 0.1)
                return True
            except Full:
                continue

    def _instance_worker(self, worker_id: int, queue: mp.JoinableQueue, lock, rx: Connection) -> None:
        Tqdm.set_lock(lock)
        try:
            self.reader._set_worker_info(WorkerInfo(self.num_workers, worker_id))
            instances = self.reader.read(self.data_path)
            checked_for_token_indexers: bool = False
            for instance in instances:
                # Check the first instance to make sure it doesn't contain any TextFields with
                # token_indexers because we don't want to be duplicating those by sending
                # them across processes.
                if not checked_for_token_indexers:
                    for field_name, field in instance.fields.items():
                        if isinstance(field, TextField) and field._token_indexers is not None:
                            raise ValueError(
                                f"Found a TextField ({field_name}) with token_indexers already "
                                "applied, but you're using num_workers > 0 in your data loader. "
                                "Make sure your dataset reader's text_to_instance() method doesn't "
                                "add any token_indexers to the TextFields it creates. Instead, the token_indexers "
                                "should be added to the instances in the apply_token_indexers() method of your "
                                "dataset reader (which you'll have to implement if you haven't done "
                                "so already)."
                            )
                    checked_for_token_indexers = True
                if self._safe_queue_put(worker_id, (instance, None), queue, rx):
                    continue
                else:
                    # Couldn't put item on queue because parent process has exited.
                    return
        except Exception as e:
            if not self._safe_queue_put(worker_id, (None, (repr(e), traceback.format_exc())), queue, rx):
                return

        # Indicate to the consumer that this worker is finished.
        queue.put((None, None))

        # Wait until this process can safely exit.
        queue.join()

    def _batch_worker(self, worker_id: int, queue: mp.JoinableQueue, lock, rx: Connection) -> None:
        Tqdm.set_lock(lock)
        try:
            self.reader._set_worker_info(WorkerInfo(self.num_workers, worker_id))
            instances = self.reader.read(self.data_path)
            for batch in self._instances_to_batches(instances, move_to_device=self._worker_cuda_safe):
                if self._safe_queue_put(worker_id, (batch, None), queue, rx):
                    continue
                else:
                    # Couldn't put item on queue because parent process has exited.
                    return
        except Exception as e:
            if not self._safe_queue_put(worker_id, (None, (repr(e), traceback.format_exc())), queue, rx):
                return

        # Indicate to the consumer (main thread) that this worker is finished.
        queue.put((None, None))

        # Wait until this process can safely exit.
        queue.join()

    def _gather_instances(self, queue: mp.JoinableQueue) -> Iterable[Instance]:
        done_count: int = 0
        while done_count < self.num_workers:
            for instance, worker_error in iter(queue.get, (None, None)):
                if worker_error is not None:
                    e, tb = worker_error
                    raise WorkerError(e, tb)

                self.reader.apply_token_indexers(instance)
                if self._vocab is not None:
                    instance.index_fields(self._vocab)
                yield instance
                queue.task_done()
            done_count += 1

    def _index_instance(self, instance: Instance) -> Instance:
        self.reader.apply_token_indexers(instance)
        assert self._vocab is not None
        instance.index_fields(self._vocab)
        return instance

    def _instances_to_batches(self, instance_iterator: Iterable[Instance], move_to_device) -> Iterator[TensorDict]:
        instance_iterator = (self._index_instance(instance) for instance in instance_iterator)

        if move_to_device and self.cuda_device is not None:
            tensorize = lambda batch: nn_util.move_to_device(self.collate_fn(batch), self.cuda_device)  # noqa: E731
        else:
            tensorize = self.collate_fn

        if self.batch_sampler is not None:
            instance_chunks: Iterable[List[Instance]]

            if self.max_instances_in_memory is not None:
                instance_chunks = lazy_groups_of(instance_iterator, self.max_instances_in_memory)
            else:
                instance_chunks = [list(instance_iterator)]

            for instances in instance_chunks:
                batches = (
                    [instances[i] for i in batch_indices]
                    for batch_indices in self.batch_sampler.get_batch_indices(instances)
                )
                for batch in batches:
                    yield tensorize(batch)
        else:
            # Safe to assume this is not `None` when `self.batch_sampler` is `None`.
            assert self.batch_size is not None

            if self.shuffle:
                if self.max_instances_in_memory is not None:
                    instance_iterator = shuffle_iterable(
                        instance_iterator,
                        self.max_instances_in_memory,
                    )
                else:
                    # At this point we've already loaded the instances in memory and indexed them,
                    # so this won't take long.
                    instance_iterator = list(instance_iterator)
                    random.shuffle(instance_iterator)

            for batch in lazy_groups_of(instance_iterator, self.batch_size):
                if self.drop_last and len(batch) < self.batch_size:
                    break
                yield tensorize(batch)

    def _maybe_tqdm(self, iterator: Iterable[_T], **tqdm_kwargs) -> Iterable[_T]:
        if self.quiet:
            return iterator
        return Tqdm.tqdm(iterator, **tqdm_kwargs)


class WorkerError(Exception):
    """
    An error raised when a worker fails.
    """

    def __init__(self, original_err_repr: str, traceback: List[str]) -> None:
        super().__init__(
            f"worker raised {original_err_repr}\n\n"
            "  Traceback from worker:\n  " + "".join(traceback)
            # Remove the first line of the traceback since it's redundant.
            .replace("Traceback (most recent call last):\n", "")
            # Give a little indentation so it's clear this traceback is separate from the traceback
            # in the main process.
            .replace("\n", "\n  ")
        )
