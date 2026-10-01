"""User-approved task policy, separate from frozen research/enrollment identity."""
from .manifest import canonical, sha256

TASK_CPU_CORE_HOURS = 128 * 72
TASK_CPU_CORE_SECONDS = TASK_CPU_CORE_HOURS * 3600
POLICY_RECORD = {
    'approved_on': '2026-09-30',
    'task_core_seconds': TASK_CPU_CORE_SECONDS,
    'gpu_allowed': False,
    'scope': 'Cumulative CPU time including failed runs and retries; historical charges retained.',
}
APPROVAL_SHA256 = sha256(canonical(POLICY_RECORD))


def description():
    return ('每个任务累计最多 9,216 CPU 核时（128×72），包含失败与重试；禁止使用 GPU。'
            '单作业核数、内存、时限及并发仍按已批准的 HPC 配置与集群硬限制执行。')
