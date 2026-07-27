"""Model-runtime integration boundaries.

Concrete backends are intentionally not imported here: the scientific session
depends on resume contracts and must remain independent of optional runtimes.
"""

from scagent_sdk.runtime.resume import ResumeMode, ResumePlan, plan_resume

__all__ = ["ResumeMode", "ResumePlan", "plan_resume"]
