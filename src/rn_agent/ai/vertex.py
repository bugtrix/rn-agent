"""Claude models, reached with a Google account instead of an Anthropic key.

This is the one legitimate way to run Claude in this agent without an Anthropic
API key: Anthropic publishes its models on Google Cloud, and Google Cloud
authenticates with OAuth. So the developer signs in with a browser - the flow
they actually wanted - and the requests are billed to their own Cloud project.

What it is not: a way to spend a Claude.ai subscription. Anthropic reserves
subscription OAuth for its own products, and no code here pretends otherwise.
This is a different product (Claude on Google Cloud) with a different bill.

Two shape differences from ``api.anthropic.com``, both handled here rather than
leaking into callers (documented by Anthropic and Google):

* the model is part of the URL, not the body, and the path is
  ``/v1/projects/<project>/locations/<location>/publishers/anthropic/models/<model>:rawPredict``;
* the body carries ``anthropic_version: "vertex-2023-10-16"`` and **no** ``model``
  field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import ProviderError
from .provider import AIProvider, ProviderIdentity
from .types import Completion, Message

#: The API version Vertex expects in the body. Pinned: it is part of the request
#: contract, not a preference.
ANTHROPIC_VERSION = "vertex-2023-10-16"

#: Google's multi-region endpoint. A specific region (``us-east5``) is also
#: valid and is what ``--region`` produces.
GLOBAL_HOST = "https://aiplatform.googleapis.com"
GLOBAL_LOCATION = "global"


def regional_host(region: str) -> str:
    """``us-east5`` -> the regional aiplatform host."""
    if region in ("", GLOBAL_LOCATION):
        return GLOBAL_HOST
    return f"https://{region}-aiplatform.googleapis.com"


class VertexAnthropicProvider(AIProvider):
    """Claude on Google Cloud, authenticated with a Google OAuth token."""

    name: ClassVar[str] = "vertex"
    label: ClassVar[str] = "Claude on Vertex AI (Google account)"
    #: There is no Vertex-specific key: the credential is a Google OAuth token,
    #: which is why this provider exists at all.
    env_var: ClassVar[str | None] = None
    requires_credential: ClassVar[bool] = True
    #: Vertex model ids carry a release suffix (``claude-sonnet-4-5@20250929``)
    #: and the available set depends on the project's Model Garden access, so
    #: there is nothing honest to hard-code. `/model <id>` names one explicitly.
    suggested_models: ClassVar[tuple[str, ...]] = ()
    default_model: ClassVar[str] = ""
    default_base_url: ClassVar[str] = GLOBAL_HOST
    docs_url: ClassVar[str] = (
        "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude"
    )
    unreachable_hint: ClassVar[str | None] = (
        "Check that the Vertex AI API is enabled in your project and that the "
        "Claude model is granted in Model Garden."
    )
    model_hint: ClassVar[str | None] = (
        "Vertex model ids carry a release date, for example "
        "`rn-agent model claude-sonnet-4-5@20250929`. Which ones you may call "
        "depends on what your project was granted in Model Garden."
    )

    def __init__(
        self,
        *,
        project: str | None = None,
        region: str = GLOBAL_LOCATION,
        **extra: Any,
    ) -> None:
        # The base resolves the credential first, so a missing Google session is
        # reported before anything project-specific is validated.
        base_url = extra.pop("base_url", None) or regional_host(region)
        super().__init__(base_url=base_url, **extra)
        self.project = (project or "").strip()
        self.region = region or GLOBAL_LOCATION

    # -- request shape -----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._credential}",
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _completion_path(self, model: str) -> str:
        if not self.project:
            raise ProviderError(
                "no Google Cloud project set for Vertex AI",
                hint=(
                    "Run `rn-agent login vertex --cloud-project <project-id>`, or set "
                    "ai.project in .rn-agent/config.yaml. Vertex requests are billed "
                    "to that project."
                ),
            )
        return (
            f"/v1/projects/{self.project}/locations/{self.region}"
            f"/publishers/anthropic/models/{model}:rawPredict"
        )

    def _payload(
        self,
        messages: list[Message],
        *,
        model: str,
        system: str | None,
        max_output_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        # `model` is deliberately unused: Vertex takes it in the URL, and sending
        # it in the body is an error rather than a redundancy.
        _ = model
        system_text, chat = self._split_system(messages, system)
        payload: dict[str, Any] = {
            "anthropic_version": ANTHROPIC_VERSION,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "messages": [message.as_dict() for message in chat],
        }
        if system_text:
            payload["system"] = system_text
        return payload

    def _parse_completion(
        self, body: Mapping[str, Any], *, model: str, task: str | None
    ) -> Completion:
        """Identical to the Messages API response, which is the point."""
        blocks = body.get("content")
        chunks: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        stop = body.get("stop_reason")
        return Completion(
            text="".join(chunks),
            provider=self.name,
            model=model,
            usage=self._usage(body, input_key="input_tokens", output_key="output_tokens"),
            stop_reason=stop if isinstance(stop, str) else None,
            task=task,
        )

    # -- catalogue ---------------------------------------------------------
    def list_models(self) -> tuple[str, ...]:
        """Vertex publishes no catalogue endpoint for partner models.

        Returning ``()`` rather than a bundled list is the honest answer: which
        Claude models a project may call depends on what has been granted in
        Model Garden, and this agent does not guess at entitlements.
        """
        return ()

    def verify(self) -> ProviderIdentity:
        """Confirm the credential and the project are present, without billing.

        Every Vertex call to a Claude model is a billable prediction, and
        ``rawPredict`` has no free probe, so verification stops at "we have a
        Google session and a project". It says so rather than implying a live
        check happened.
        """
        detail = f"Google session present; project {self.project}" if self.project else ""
        if not self.project:
            raise ProviderError(
                "no Google Cloud project set for Vertex AI",
                hint=(
                    "Run `rn-agent login vertex --cloud-project <project-id>` - that "
                    "project is what pays."
                ),
            )
        return ProviderIdentity(
            ok=True,
            provider=self.name,
            models=(),
            detail=(
                f"{detail} - not verified against the API: every Vertex Claude "
                "request is billable, so there is no free probe"
            ),
        )
