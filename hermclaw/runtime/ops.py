"""Operations tools of the runtime (P26–P28): one :class:`CompositeOpsProvider` for the coder's ``ssh``/``deploy``/
``database``/``docker`` steps, built from the configuration.

* SSH (``hermclaw.sshtools``): ``ssh_read`` / ``ssh_command`` / ``ssh_upload`` / ``ssh_service_status`` against the
  allow-listed hosts of ``hosts.yaml`` + ``policies.ssh`` (approvals for mutations, audited runner). Its runner is also
  the remote command runner of the Wake-on-LAN idle-sleep hook.
* Database and container/proxy providers are added when their packages provide an ``OpsToolProvider``
  (``hermclaw.dbtools.provider`` / ``hermclaw.infratools.provider``); a missing optional package only disables those
  tools (the tool engine answers ``OPS_TOOL_UNAVAILABLE``).

Which tools a step may call is decided by the tool engine from the step's capability (``OPS_TOOLS_BY_CAPABILITY``),
never by the provider.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermclaw.core.config import HermclawConfig
from hermclaw.core.logging import get_logger
from hermclaw.core.settings import Settings
from hermclaw.tools.ops import CompositeOpsProvider, OpsToolProvider

log = get_logger(__name__)

#: optional providers: module -> factory ``build_provider(config, sessionmaker, settings) -> OpsToolProvider``
OPTIONAL_PROVIDERS = ("hermclaw.dbtools.provider", "hermclaw.infratools.provider")


@dataclass
class OpsBundle:
    provider: OpsToolProvider | None
    ssh_runner: Any = None  # hermclaw.sshtools.client.SshRunner (RemoteCommandRunner) or None
    ssh_tools: Any = None  # hermclaw.sshtools.tools.SshTools (operator approvals, audit)
    names: list[str] = field(default_factory=list)


def build_ops(config: HermclawConfig, sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> OpsBundle:
    providers: list[OpsToolProvider] = []
    names: list[str] = []
    ssh_tools = ssh_runner = None
    from hermclaw.sshtools.provider import SshOpsProvider
    from hermclaw.sshtools.tools import SshTools

    # materialised cred: keys go to $RUNTIME_DIRECTORY (systemd RuntimeDirectory=, tmpfs) – hermclaw.sshtools.keys
    ssh_tools = SshTools.from_config(config, sessionmaker, compound_allowlist=config.policies.ssh.compound_allowlist)
    ssh_runner = ssh_tools.runner
    providers.append(SshOpsProvider.for_config(ssh_tools, config))
    names.append("ssh")
    for module_name in OPTIONAL_PROVIDERS:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and module_name.startswith(exc.name):
                log.info("operations provider not installed", extra={"provider_module": module_name})
                continue
            raise
        provider = module.build_provider(config, sessionmaker, settings)
        if provider is not None:
            providers.append(provider)
            names.append(module_name.split(".")[1])
    return OpsBundle(CompositeOpsProvider(providers), ssh_runner=ssh_runner, ssh_tools=ssh_tools, names=names)


__all__ = ["OPTIONAL_PROVIDERS", "OpsBundle", "build_ops"]
