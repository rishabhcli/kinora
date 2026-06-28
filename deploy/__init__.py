"""Kinora deployment artifacts (kinora.md §12.6).

* :mod:`deploy.alibaba_render_worker` — the proof-of-deployment Alibaba Cloud
  render worker (OSS + DashScope + ECS/FC).
* :mod:`deploy.orchestrator` — the cloud-agnostic deployment orchestration
  service (blue-green / canary rollout, SLO-gated auto-rollback, drain
  coordination, a deterministic simulator).
"""
