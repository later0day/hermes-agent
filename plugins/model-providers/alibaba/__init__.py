"""Alibaba Cloud DashScope provider profiles (intl + CN, plus the Model Studio
Token Plan flat-token tier with its own key/endpoints — one module per vendor).

Profile names match models.dev catalog keys exactly so model metadata lines up
and ``model.provider: alibaba-cn`` resolves at runtime.
"""

from typing import Any

from agent.reasoning_effort import dashscope_preserve_thinking_extras
from providers import register_provider
from providers.base import ProviderProfile


class DashScopeProfile(ProviderProfile):
    """DashScope compatible-mode: ask the server to keep replayed prior-turn reasoning."""

    def build_api_kwargs_extras(self, *, model: str | None = None, **context: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        return dashscope_preserve_thinking_extras(model), {}

    def default_vision_model(self) -> str | None:
        """Vision backend inherited by text-only DashScope chat models (``qwen-plus``) when
        ``auxiliary.vision`` is left on auto. ``kimi-k3`` read every cell of a dense table screenshot
        correctly where ``qwen3-vl-plus`` / the omni models misread them, and ``qwen-vl-max`` is no
        longer served to every account. Token Plan tiers expose their own catalog, so they keep the
        generic fallback."""
        return "kimi-k3" if self.name in {"alibaba", "alibaba-cn"} else None


alibaba = DashScopeProfile(
    name="alibaba", aliases=("dashscope", "alibaba-cloud", "qwen-dashscope", "aliyun"), env_vars=("DASHSCOPE_API_KEY",),
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
)

alibaba_cn = DashScopeProfile(
    name="alibaba-cn", aliases=("dashscope-cn", "alibaba-cloud-cn"),
    display_name="Alibaba Cloud DashScope (China)",
    description="Alibaba Cloud DashScope, mainland-China endpoint",
    env_vars=("DASHSCOPE_API_KEY", "DASHSCOPE_CN_BASE_URL"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

alibaba_token_plan = DashScopeProfile(
    name="alibaba-token-plan", aliases=("dashscope-token-plan",), display_name="Alibaba Cloud (Token Plan)",
    description="Alibaba Cloud Model Studio Token Plan (flat-token tier)",
    signup_url="https://help.aliyun.com/zh/model-studio/",
    env_vars=("ALIBABA_TOKEN_PLAN_API_KEY", "ALIBABA_TOKEN_PLAN_BASE_URL"),
    base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", auth_type="api_key",
)

alibaba_token_plan_cn = DashScopeProfile(
    name="alibaba-token-plan-cn", aliases=("dashscope-token-plan-cn",),
    display_name="Alibaba Cloud (Token Plan, China)",
    description="Alibaba Cloud Model Studio Token Plan, mainland-China endpoint",
    signup_url="https://help.aliyun.com/zh/model-studio/",
    env_vars=("ALIBABA_TOKEN_PLAN_CN_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY", "ALIBABA_TOKEN_PLAN_CN_BASE_URL"),
    base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", auth_type="api_key",
)

register_provider(alibaba)
register_provider(alibaba_cn)
register_provider(alibaba_token_plan)
register_provider(alibaba_token_plan_cn)
