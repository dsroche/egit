#!/usr/bin/env python3
import os
import sys
import json
import argparse
from pathlib import Path
from typing import TypedDict, cast
import subprocess

# ==========================================
# 0. Type Definitions (mypy --strict)
# ==========================================
class ProjectConfig(TypedDict):
    ciphertext_dir: str

class GlobalConfig(TypedDict):
    default: str | None
    projects: dict[str, ProjectConfig]

class LocalConfig(TypedDict):
    remote_host: str
    remote_path: str
    staging_keyfile: str

class ProjectPaths(TypedDict):
    working_cipher: Path
    staging_cipher: Path
    working_plain: Path
    staging_plain: Path
    local_config: Path

class EgitArgs(argparse.Namespace):
    project: str | None
    force: bool
    command: str
    remote: bool

# ==========================================
# 1. XDG Path Resolution
# ==========================================
def get_xdg_paths() -> tuple[Path, Path, Path]:
    config_home: str = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    config_dir: Path = Path(config_home) / "egit"

    data_home: str = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    vaults_dir: Path = Path(data_home) / "egit" / "vaults"

    uid: int = os.getuid()
    runtime_dir: str = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    run_dir: Path = Path(runtime_dir) / "egit"

    return config_dir, vaults_dir, run_dir

CONFIG_DIR: Path
VAULTS_DIR: Path
RUN_DIR: Path
CONFIG_DIR, VAULTS_DIR, RUN_DIR = get_xdg_paths()

GLOBAL_CONFIG_FILE: Path = CONFIG_DIR / "config.json"

# ==========================================
# 2. Configuration Management
# ==========================================
def load_global_config() -> GlobalConfig:
    if not GLOBAL_CONFIG_FILE.exists():
        return {"default": None, "projects": {}}
    try:
        with open(GLOBAL_CONFIG_FILE, "r") as f:
            return cast(GlobalConfig, json.load(f))
    except json.JSONDecodeError:
        print(f"Error: {GLOBAL_CONFIG_FILE} is corrupted or not valid JSON.", file=sys.stderr)
        sys.exit(1)

def save_global_config(config_data: GlobalConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(GLOBAL_CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=2)

def get_project_paths(project_name: str, config_data: GlobalConfig) -> ProjectPaths:
    projects: dict[str, ProjectConfig] = config_data.get("projects", {})
    if project_name not in projects:
        print(f"Error: Project '{project_name}' not found in config.", file=sys.stderr)
        sys.exit(1)

    base_cipher: Path = Path(projects[project_name]["ciphertext_dir"])
    base_plain: Path = RUN_DIR / project_name

    return {
        "working_cipher": base_cipher / "working",
        "staging_cipher": base_cipher / "staging",
        "working_plain": base_plain / "working",
        "staging_plain": base_plain / "staging",
        "local_config": base_plain / "working" / ".egit.json"
    }

def resolve_single_project(args: EgitArgs, config: GlobalConfig) -> tuple[str, ProjectPaths]:
    """Helper to resolve the target project for commands that require exactly one."""
    project_name: str | None = args.project or config.get("default")
    if not project_name:
        print("Error: No default project set and no --project specified.", file=sys.stderr)
        sys.exit(1)

    paths: ProjectPaths = get_project_paths(project_name, config)
    return project_name, paths

# ==========================================
# 3. Command Handlers
# ==========================================
def handle_status(args: EgitArgs, config: GlobalConfig) -> None:
    target_projects: list[str] = []

    if not args.project:
        target_projects = list(config.get("projects", {}).keys())
        if not target_projects:
            print("No projects configured yet.")
            return
    else:
        target_projects = [args.project]

    print(f"{'PROJECT':<20} {'WORKING':<15} {'STAGING':<15} {'STATE'}")
    print("-" * 65)

    for p in target_projects:
        paths: ProjectPaths = get_project_paths(p, config)

        working_status: str = get_mount_state(paths["working_plain"])
        staging_status: str = get_mount_state(paths["staging_plain"])

        # We can only know the dirty state if Working is decrypted and mounted
        dirty_state: str = "UNKNOWN"
        if working_status != "Unmounted":
            dirty_state = "DIRTY" if is_dirty(paths) else "CLEAN"

        print(f"{p:<20} {working_status:<15} {staging_status:<15} {dirty_state}")

        if args.remote:
            check_remote_status(paths)

def handle_create(args: EgitArgs, config: GlobalConfig) -> None:
    print("Executing: create")
    # TODO: Implement initialization logic

def handle_open(args: EgitArgs, project_name: str, paths: ProjectPaths) -> None:
    print(f"Executing: open on {project_name}")
    # TODO: Implement mount, pull, and read-only staging logic

def handle_close(args: EgitArgs, project_name: str, paths: ProjectPaths) -> None:
    print(f"Executing: close on {project_name}")
    # TODO: Implement safe unmount logic

# ==========================================
# 4. FUSE & Filesystem Helpers
# ==========================================
def get_mount_state(mount_point: Path) -> str:
    """Returns 'Unmounted', 'Mounted (R/W)', or 'Mounted (RO)'."""
    if not os.path.ismount(mount_point):
        return "Unmounted"

    # Parse /proc/mounts to find the specific mount options
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts: list[str] = line.split()
                if len(parts) >= 4:
                    # /proc/mounts replaces spaces with \040
                    mpt: str = parts[1].replace("\\040", " ")
                    if mpt == str(mount_point.resolve()):
                        opts: list[str] = parts[3].split(",")
                        if "ro" in opts:
                            return "Mounted (RO)"
                        return "Mounted (R/W)"
    except Exception:
        pass

    return "Mounted (???)"

def is_dirty(paths: ProjectPaths) -> bool:
    """Checks for the existence of the DIRTY_STAGING flag."""
    return (paths["working_plain"] / "DIRTY_STAGING").exists()

def check_remote_status(paths: ProjectPaths) -> None:
    """Helper to check remote lock and rsync drift (Requires Working to be mounted)."""
    if not os.path.ismount(paths["working_plain"]):
        print("  └─ Remote: Cannot check remote. Working directory is unmounted (config inaccessible).\n")
        return

    try:
        local_config: LocalConfig = load_local_config(paths["local_config"])
    except SystemExit:
        print("  └─ Remote: Error reading local config.\n")
        return

    remote_host: str = local_config["remote_host"]
    remote_path: str = local_config["remote_path"]
    lock_path: str = f"{remote_path}/lock"
    staging_remote: str = f"{remote_path}/staging/"

    # --- Check SSH Lock ---
    print("  └─ Checking server lock... ", end="", flush=True)
    lock_cmd: list[str] = ["ssh", "-o", "BatchMode=yes", remote_host, f"cat {lock_path}/info 2>/dev/null"]
    try:
        result = subprocess.run(lock_cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            print(f"LOCKED -> {result.stdout.strip()}")
        else:
            print("UNLOCKED")
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
    except Exception as e:
        print(f"ERROR ({e})")

    # --- Check Ciphertext Drift ---
    print("  └─ Checking upstream drift... ", end="", flush=True)
    # Using -ani: archive, dry-run, itemize-changes
    rsync_cmd: list[str] = [
        "rsync", "-ani", "--delete",
        f"{remote_host}:{staging_remote}",
        str(paths["staging_cipher"]) + "/"
    ]
    try:
        rsync_result = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=15)
        if rsync_result.returncode == 0:
            # Filter out empty lines and non-file noise
            changes: list[str] = [line for line in rsync_result.stdout.splitlines() if line.strip()]
            if changes:
                print(f"DRIFT DETECTED ({len(changes)} files differ)")
            else:
                print("IN SYNC")
        else:
            print("ERROR (rsync connection failed)")
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
    except Exception as e:
        print(f"ERROR ({e})")

    print()  # Add a blank line for readability between projects

# ==========================================
# 5. CLI Entrypoint
# ==========================================
def main() -> None:
    parser = argparse.ArgumentParser(description="egit - Encrypted Git Vault Manager")

    parser.add_argument("-p", "--project", type=str, default=None, help="Specify project name (overrides default)")
    parser.add_argument("-f", "--force", action="store_true", help="Force operation, overriding safety checks")

    subparsers = parser.add_subparsers(dest="command", required=True, title="Commands")

    cmd_create = subparsers.add_parser("create", help="Create a new encrypted project")
    cmd_bootstrap = subparsers.add_parser("bootstrap", help="Setup existing project on a new machine")

    cmd_open = subparsers.add_parser("open", help="Mount vaults and pull changes")
    cmd_close = subparsers.add_parser("close", help="Safely unmount vaults")

    cmd_push = subparsers.add_parser("push", help="Safe macro: push local changes to remote")
    cmd_dirty = subparsers.add_parser("dirty", help="Lock remote, sync down, mount Staging R/W")
    cmd_write = subparsers.add_parser("write", help="Unmount Staging R/W, sync up, unlock")

    cmd_status = subparsers.add_parser("status", help="Check local mount and transaction states")
    cmd_status.add_argument("--remote", action="store_true", default=False, help="Check remote lock and drift")

    args: EgitArgs = parser.parse_args(namespace=EgitArgs())
    config: GlobalConfig = load_global_config()

    # Route to the appropriate handler
    if args.command == "status":
        handle_status(args, config)
    elif args.command == "create":
        handle_create(args, config)
    elif args.command == "bootstrap":
        # handle_bootstrap(args, config)
        print("Executing: bootstrap")
    else:
        project_name, paths = resolve_single_project(args, config)
        if args.command == "open":
            handle_open(args, project_name, paths)
        elif args.command == "close":
            handle_close(args, project_name, paths)
        elif args.command == "push":
            print(f"Executing: push on {project_name}")
        elif args.command == "dirty":
            print(f"Executing: dirty on {project_name}")
        elif args.command == "write":
            print(f"Executing: write on {project_name}")

if __name__ == "__main__":
    main()
