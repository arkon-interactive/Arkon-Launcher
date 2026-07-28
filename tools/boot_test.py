"""Headless end-to-end check: provision a world and boot it to 'Done'.

Deliberately not part of the app. This is the harness for the riskiest part of
the project - whether an arbitrary client modpack survives being run as a
server - so it can be re-run against any instance without the UI in the way.

    python tools/boot_test.py [world-folder-name]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arkon_launcher import instances, modsync, paths, provision, worlds  # noqa: E402
from arkon_launcher.runner import ServerConfig, ServerProcess, ServerState  # noqa: E402


def main(world_name: str | None) -> int:
    found = instances.find_instances()
    if not found:
        print("No CurseForge instances found.")
        return 1

    instance = found[0]
    print(f"Instance : {instance.name}")
    print(f"Minecraft: {instance.mc_version} / Fabric {instance.loader_version}")

    unsupported = instances.describe_unsupported(instance)
    if unsupported:
        print(unsupported)
        return 1

    available = worlds.find_worlds(instance.saves_dir)
    if world_name:
        world = next((w for w in available if w.folder_name == world_name), None)
    else:
        world = next((w for w in available if not worlds.is_world_busy(w.folder)), None)

    if world is None:
        print(f"World not found or all worlds are open in Minecraft.")
        return 1

    if worlds.is_world_busy(world.folder):
        print(f"'{world.folder_name}' is open in Minecraft right now. Close it first.")
        return 1

    print(f"World    : {world.folder_name} ({world.player_layout.value} player layout)")

    server_dir = paths.server_dir(instance.directory, world.folder_name)

    java = provision.select_java(instance)
    print(f"Java     : {java.display}")

    print("Syncing mods...")
    result = modsync.select_server_mods(instance.mods_dir)
    modsync.mirror_mods(result, server_dir / "mods")
    modsync.mirror_tree(instance.config_dir, server_dir / "config")
    modsync.mirror_tree(instance.directory / "defaultconfigs", server_dir / "defaultconfigs")
    print(f"           {result.summary()}")

    modsync.remove_legacy_world_link(server_dir)
    universe, world_folder = modsync.world_container(world.folder)
    provision.ensure_server_properties(
        server_dir, world.level_name, level_name=world_folder
    )

    if not provision.eula_accepted(server_dir):
        print(f"EULA not accepted. See {provision.EULA_URL}")
        return 1

    jar = provision.ensure_server_jar(instance)
    print(f"Server   : {jar.name}\n")

    config = ServerConfig(
        java=java.executable,
        server_jar=jar,
        working_dir=server_dir,
        max_memory_mb=6144,
        extra_jvm_args=runner_args(instance),
        universe=universe,
        world_name=world_folder,
    )

    server = ServerProcess(config)
    server.on_line(lambda line: print(line, flush=True))

    started = time.time()
    server.start()
    ready = server.wait_for_ready(timeout=900)

    if not ready:
        print(f"\n--- FAILED to reach 'Done' (state={server.state.value}, exit={server.exit_code})")
        return 2

    print(f"\n--- Server ready in {time.time() - started:.1f}s")

    for command in ("list", "seed"):
        print(f"--- sending: {command}")
        server.send(command)
        time.sleep(2)

    print("--- stopping")
    code = server.stop()
    print(f"--- exited with {code}, final state {server.state.value}")
    return 0 if server.state is ServerState.STOPPED else 3


def runner_args(instance) -> list[str]:
    from arkon_launcher.runner import sanitize_jvm_args

    return sanitize_jvm_args(instance.java_args_override)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
