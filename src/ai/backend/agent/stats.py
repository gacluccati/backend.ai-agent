"""
A module to collect various performance metrics of Docker containers.

Reference: https://www.datadoghq.com/blog/how-to-collect-docker-metrics/
"""

import asyncio
from decimal import Decimal
import enum
import logging
import sys
import time
from typing import (
    Optional,
    Callable,
    List, Tuple,
    Dict, Mapping, MutableMapping,
    FrozenSet,
    TYPE_CHECKING,
)

import attr

from ai.backend.common import redis
from ai.backend.common.identity import is_containerized
from ai.backend.common.logging import BraceStyleAdapter
from ai.backend.common import msgpack
from ai.backend.common.types import (
    ContainerId, DeviceId, KernelId,
    MetricKey, MetricValue, MovingStatValue,
)
from .utils import (
    remove_exponent,
)
if TYPE_CHECKING:
    from .agent import AbstractAgent

__all__ = (
    'StatContext',
    'StatModes',
    'MetricTypes',
    'NodeMeasurement',
    'ContainerMeasurement',
    'Measurement',
)

log = BraceStyleAdapter(logging.getLogger('ai.backend.agent.stats'))


def check_cgroup_available():
    """
    Check if the host OS provides cgroups.
    """
    return (not is_containerized() and sys.platform.startswith('linux'))


class StatModes(enum.Enum):
    CGROUP = 'cgroup'
    DOCKER = 'docker'

    @staticmethod
    def get_preferred_mode():
        """
        Returns the most preferred statistics collector type for the host OS.
        """
        if check_cgroup_available():
            return StatModes.CGROUP
        return StatModes.DOCKER


class MetricTypes(enum.Enum):
    USAGE = 0        # for instant snapshot (e.g., used memory bytes, used cpu msec)
    RATE = 1         # for rate of increase (e.g., I/O bps)
    UTILIZATION = 2  # for ratio of resource occupation time per measurement interval (e.g., CPU util)
    ACCUMULATED = 3  # for accumulated value (e.g., total number of events)


@attr.s(auto_attribs=True, slots=True)
class Measurement:
    value: Decimal
    capacity: Optional[Decimal] = None


@attr.s(auto_attribs=True, slots=True)
class NodeMeasurement:
    """
    Collection of per-node and per-agent statistics for a specific metric.
    """
    # 2-tuple of Decimals mean raw values for (usage, available)
    # Percent values are calculated from them.
    key: str
    type: MetricTypes
    per_node: Measurement
    per_device: Mapping[DeviceId, Measurement] = attr.Factory(dict)
    unit_hint: Optional[str] = None
    stats_filter: FrozenSet[str] = attr.Factory(frozenset)
    current_hook: Optional[Callable[['Metric'], Decimal]] = None


@attr.s(auto_attribs=True, slots=True)
class ContainerMeasurement:
    """
    Collection of per-container statistics for a specific metric.
    """
    key: str
    type: MetricTypes
    per_container: Mapping[str, Measurement] = attr.Factory(dict)
    unit_hint: Optional[str] = None
    stats_filter: FrozenSet[str] = attr.Factory(frozenset)
    current_hook: Optional[Callable[['Metric'], Decimal]] = None


class MovingStatistics:
    __slots__ = (
        '_sum', '_count',
        '_min', '_max', '_last',
    )
    _sum: Decimal
    _count: int
    _min: Decimal
    _max: Decimal
    _last: List[Tuple[Decimal, float]]

    def __init__(self, initial_value: Decimal = None):
        self._last = []
        if initial_value is None:
            self._sum = Decimal(0)
            self._min = Decimal('inf')
            self._max = Decimal('-inf')
            self._count = 0
        else:
            self._sum = initial_value
            self._min = initial_value
            self._max = initial_value
            self._count = 1
            point = (initial_value, time.perf_counter())
            self._last.append(point)

    def update(self, value: Decimal):
        self._sum += value
        self._min = min(self._min, value)
        self._max = max(self._max, value)
        self._count += 1
        point = (value, time.perf_counter())
        self._last.append(point)
        # keep only the latest two data points
        if len(self._last) > 2:
            self._last.pop(0)

    @property
    def min(self) -> Decimal:
        return self._min

    @property
    def max(self) -> Decimal:
        return self._max

    @property
    def sum(self) -> Decimal:
        return self._sum

    @property
    def avg(self) -> Decimal:
        return self._sum / self._count

    @property
    def diff(self) -> Decimal:
        if len(self._last) == 2:
            return self._last[-1][0] - self._last[-2][0]
        return Decimal(0)

    @property
    def rate(self) -> Decimal:
        if len(self._last) == 2:
            return ((self._last[-1][0] - self._last[-2][0]) /
                    Decimal(self._last[-1][1] - self._last[-2][1]))
        return Decimal(0)

    def to_serializable_dict(self) -> MovingStatValue:
        q = Decimal('0.000')
        return {
            'min': str(remove_exponent(self.min.quantize(q))),
            'max': str(remove_exponent(self.max.quantize(q))),
            'sum': str(remove_exponent(self.sum.quantize(q))),
            'avg': str(remove_exponent(self.avg.quantize(q))),
            'diff': str(remove_exponent(self.diff.quantize(q))),
            'rate': str(remove_exponent(self.rate.quantize(q))),
            'version': 2,
        }


@attr.s(auto_attribs=True, slots=True)
class Metric:
    key: str
    type: MetricTypes
    stats: MovingStatistics
    stats_filter: FrozenSet[str]
    current: Decimal
    capacity: Optional[Decimal] = None
    unit_hint: Optional[str] = None
    current_hook: Optional[Callable[['Metric'], Decimal]] = None

    def update(self, value: Measurement):
        if value.capacity is not None:
            self.capacity = value.capacity
        self.stats.update(value.value)
        self.current = value.value
        if self.current_hook is not None:
            self.current = self.current_hook(self)

    def to_serializable_dict(self) -> MetricValue:
        q = Decimal('0.000')
        q_pct = Decimal('0.00')
        return {
            'current': str(remove_exponent(self.current.quantize(q))),
            'capacity': (str(remove_exponent(self.capacity.quantize(q)))
                         if self.capacity is not None else None),
            'pct': (
                str(remove_exponent(
                    (Decimal(self.current) / Decimal(self.capacity) * 100).quantize(q_pct)))
                if (self.capacity is not None and
                    self.capacity.is_normal() and
                    self.capacity > 0)
                else None),
            'unit_hint': self.unit_hint,
            **{f'stats.{k}': v  # type: ignore
               for k, v in self.stats.to_serializable_dict().items()
               if k in self.stats_filter},
        }


class StatContext:

    agent: 'AbstractAgent'
    mode: StatModes
    node_metrics: Mapping[MetricKey, Metric]
    device_metrics: Mapping[MetricKey, MutableMapping[DeviceId, Metric]]
    kernel_metrics: MutableMapping[KernelId, MutableMapping[MetricKey, Metric]]

    def __init__(self, agent: 'AbstractAgent', mode: StatModes = None, *,
                 cache_lifespan: float = 120.0) -> None:
        self.agent = agent
        self.mode = mode if mode is not None else StatModes.get_preferred_mode()
        self.cache_lifespan = cache_lifespan

        self.node_metrics = {}
        self.device_metrics = {}
        self.kernel_metrics = {}

        self._lock = asyncio.Lock()
        self._timestamps: MutableMapping[str, float] = {}

    def update_timestamp(self, timestamp_key: str) -> Tuple[float, float]:
        """
        Update the timestamp for the given key and return a pair of the current timestamp and
        the interval from the last update of the same key.

        If the last timestamp for the given key does not exist, the interval becomes "NaN".

        Intended to be used by compute plugins.
        """
        now = time.perf_counter()
        last = self._timestamps.get(timestamp_key, None)
        self._timestamps[timestamp_key] = now
        if last is None:
            return now, float('NaN')
        return now, now - last

    async def collect_node_stat(self):
        """
        Collect the per-node, per-device, and per-container statistics.

        Intended to be used by the agent.
        """
        async with self._lock:
            # gather node/device metrics from compute plugins
            _tasks = []
            for computer in self.agent.computers.values():
                _tasks.append(computer.instance.gather_node_measures(self))
            for node_measures in (await asyncio.gather(*_tasks, return_exceptions=True)):
                if isinstance(node_measures, Exception):
                    log.error('gather_node_measures error',
                              exc_info=node_measures)
                    continue
                for node_measure in node_measures:
                    metric_key = node_measure.key
                    # update node metric
                    if metric_key not in self.node_metrics:
                        self.node_metrics[metric_key] = Metric(
                            metric_key, node_measure.type,
                            current=node_measure.per_node.value,
                            capacity=node_measure.per_node.capacity,
                            unit_hint=node_measure.unit_hint,
                            stats=MovingStatistics(node_measure.per_node.value),
                            stats_filter=frozenset(node_measure.stats_filter),
                            current_hook=node_measure.current_hook,
                        )
                    else:
                        self.node_metrics[metric_key].update(node_measure.per_node)
                    # update per-device metric
                    # NOTE: device IDs are defined by each metric keys.
                    for dev_id, measure in node_measure.per_device.items():
                        dev_id = str(dev_id)
                        if metric_key not in self.device_metrics:
                            self.device_metrics[metric_key] = {}
                        if dev_id not in self.device_metrics[metric_key]:
                            self.device_metrics[metric_key][dev_id] = Metric(
                                metric_key, node_measure.type,
                                current=measure.value,
                                capacity=measure.capacity,
                                unit_hint=node_measure.unit_hint,
                                stats=MovingStatistics(measure.value),
                                stats_filter=frozenset(node_measure.stats_filter),
                                current_hook=node_measure.current_hook,
                            )
                        else:
                            self.device_metrics[metric_key][dev_id].update(measure)

            # gather container metrics from compute plugins
            container_ids = []
            kernel_id_map = {}
            used_kernel_ids = set()
            for kid, kobj in self.agent.kernel_registry.items():
                cid = kobj['container_id']
                container_ids.append(cid)
                kernel_id_map[cid] = kid
                used_kernel_ids.add(kid)
            unused_kernel_ids = set(self.kernel_metrics.keys()) - used_kernel_ids
            for unused_kernel_id in unused_kernel_ids:
                log.debug('removing kernel_metric for {}', unused_kernel_id)
                self.kernel_metrics.pop(unused_kernel_id, None)
            _tasks = []
            for computer in self.agent.computers.values():
                _tasks.append(computer.instance.gather_container_measures(self, container_ids))
            for ctnr_measures in (await asyncio.gather(*_tasks, return_exceptions=True)):
                if isinstance(ctnr_measures, Exception):
                    log.error('gather_container_measures error',
                              exc_info=ctnr_measures)
                    continue
                for ctnr_measure in ctnr_measures:
                    metric_key = ctnr_measure.key
                    # update per-container metric
                    for cid, measure in ctnr_measure.per_container.items():
                        kernel_id = kernel_id_map[cid]
                        if kernel_id not in self.kernel_metrics:
                            self.kernel_metrics[kernel_id] = {}
                        if metric_key not in self.kernel_metrics[kernel_id]:
                            self.kernel_metrics[kernel_id][metric_key] = Metric(
                                metric_key, ctnr_measure.type,
                                current=measure.value,
                                capacity=measure.value,
                                unit_hint=ctnr_measure.unit_hint,
                                stats=MovingStatistics(measure.value),
                                stats_filter=frozenset(ctnr_measure.stats_filter),
                                current_hook=ctnr_measure.current_hook,
                            )
                        else:
                            self.kernel_metrics[kernel_id][metric_key].update(measure)

        # push to the Redis server
        redis_agent_updates = {
            'node': {
                key: obj.to_serializable_dict()
                for key, obj in self.node_metrics.items()
            },
            'devices': {
                metric_key: {dev_id: obj.to_serializable_dict()
                             for dev_id, obj in per_device.items()}
                for metric_key, per_device in self.device_metrics.items()
            },
        }
        if self.agent.local_config['debug']['log-stats']:
            log.debug('stats: node_updates: {0}: {1}',
                      self.agent.local_config['agent']['id'], redis_agent_updates['node'])
        serialized_agent_updates = msgpack.packb(redis_agent_updates)

        def _pipe_builder():
            pipe = self.agent.redis_stat_pool.pipeline()
            pipe.set(self.agent.local_config['agent']['id'], serialized_agent_updates)
            pipe.expire(self.agent.local_config['agent']['id'], self.cache_lifespan)
            for kernel_id, metrics in self.kernel_metrics.items():
                serialized_metrics = {
                    key: obj.to_serializable_dict()
                    for key, obj in metrics.items()
                }
                pipe.set(str(kernel_id), msgpack.packb(serialized_metrics))
                pipe.expire(str(kernel_id), self.cache_lifespan)
            return pipe
        await redis.execute_with_retries(_pipe_builder)

    async def collect_container_stat(self, container_id: ContainerId) -> Mapping[MetricKey, Metric]:
        """
        Collect the per-container statistics only,

        Intended to be used by the agent and triggered by container cgroup synchronization processes.
        """
        async with self._lock:
            kernel_id_map: Dict[ContainerId, KernelId] = {}
            for kid, info in self.agent.kernel_registry.items():
                cid = info['container_id']
                kernel_id_map[ContainerId(cid)] = kid

            _tasks = []
            kernel_id = None
            for computer in self.agent.computers.values():
                _tasks.append(
                    computer.instance.gather_container_measures(self, [container_id]))
            for ctnr_measures in (await asyncio.gather(*_tasks, return_exceptions=True)):
                if isinstance(ctnr_measures, Exception):
                    log.error('gather_cotnainer_measures error',
                              exc_info=ctnr_measures)
                    continue
                for ctnr_measure in ctnr_measures:
                    metric_key = ctnr_measure.key
                    # update per-container metric
                    for cid, measure in ctnr_measure.per_container.items():
                        assert cid == container_id
                        try:
                            kernel_id = kernel_id_map[cid]
                        except KeyError:
                            continue
                        if kernel_id not in self.kernel_metrics:
                            self.kernel_metrics[kernel_id] = {}
                        if metric_key not in self.kernel_metrics[kernel_id]:
                            self.kernel_metrics[kernel_id][metric_key] = Metric(
                                metric_key, ctnr_measure.type,
                                current=measure.value,
                                capacity=measure.value,
                                unit_hint=ctnr_measure.unit_hint,
                                stats=MovingStatistics(measure.value),
                                stats_filter=frozenset(ctnr_measure.stats_filter),
                                current_hook=ctnr_measure.current_hook,
                            )
                        else:
                            self.kernel_metrics[kernel_id][metric_key].update(measure)

        if kernel_id is not None:
            metrics = self.kernel_metrics[kernel_id]
            serializable_metrics = {
                key: obj.to_serializable_dict()
                for key, obj in metrics.items()
            }
            if self.agent.local_config['debug']['log-stats']:
                log.debug('kernel_updates: {0}: {1}',
                          kernel_id, serializable_metrics)
            serialized_metrics = msgpack.packb(serializable_metrics)

            def _pipe_builder():
                pipe = self.agent.redis_stat_pool.pipeline()
                pipe.set(str(kernel_id), serialized_metrics)
                pipe.expire(str(kernel_id), self.cache_lifespan)
                return pipe
            await redis.execute_with_retries(_pipe_builder)
            return metrics
        return {}
