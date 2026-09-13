#!/usr/bin/env python3

import argparse
import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Dict
from dataclasses import dataclass, asdict
import shlex

class LockType(StrEnum):
    RO = "read-only"
    RW = "read-write"


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class LocalState(StrEnum):
    SYNCING = "SYNCING"
    LOCKED = "LOCKED"
    NONE = "NONE"


class LockSyncError(Exception):
    """Custom exception for all locksync predictable failures."""
    pass


@dataclass
class Config:
    remote_server: str
    remote_dir: str


class ConfigManager:
    CONFIG_FNAME: str = 'locksync.json'

    def __init__(self, local_path: Path) -> None:
        self.config_path = local_path / "locksync.json"

    def load(self) -> Config:
        if not self.config_path.exists():
            raise LockSyncError(f"Config not found at {self.config_path}. Run 'create' or 'join' first.")
        with self.config_path.open("r") as f:
            data = json.load(f)
        return Config(remote_server=data["remote_server"], remote_dir=data["remote_dir"])

    def create(self, server: str, remote_dir: str, force: bool) -> Config:
        if self.config_path.exists() and not force:
            raise LockSyncError(f"Config already exists at {self.config_path}. Use --force to overwrite.")
        conf = Config(remote_server=server, remote_dir=remote_dir)
        with self.config_path.open("w") as f:
            json.dump(asdict(conf), f, indent=4)
        return conf

def guess_local_folder() -> Path:
    """Tries to auto-determine the local_folder based on locksync.json location."""
    cwd = Path.cwd()
    parents = [cwd]
    parents.extend(cwd.parents)
    for p in parents:
        if (p / ConfigManager.CONFIG_FNAME).exists():
            return p
    raise LockSyncError("could not find any locksync path in the parents of current directory.")


class LocalFolderManager:
    def __init__(self, local_path: Path) -> None:
        self.local_path = local_path
        self.data_path = local_path / "data"

    def initialize(self, force: bool) -> None:
        try:
            self.local_path.mkdir(parents=True, exist_ok=force)
            self.data_path.mkdir(exist_ok=force)
        except FileExistsError as e:
            raise LockSyncError(f"Path already exists: {e.filename}. Use --force to override.")

    def get_current_state(self) -> LocalState:
        if (self.local_path / LocalState.LOCKED).exists():
            return LocalState.LOCKED
        if (self.local_path / LocalState.SYNCING).exists():
            return LocalState.SYNCING
        return LocalState.NONE

    def read_state_file(self, state: LocalState) -> Dict[str, Any]:
        path = self.local_path / state
        if not path.exists():
            raise LockSyncError(f"State file {state} not found.")
        with path.open("r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise LockSyncError(f"State file {state} contains invalid format.")
            return data

    def write_state_file(self, state: LocalState, info: Dict[str, Any]) -> Path:
        self.clear_state_files()
        dest = self.local_path / state
        with dest.open("w") as f:
            json.dump(info, f, indent=4)
        return dest

    def clear_state_files(self) -> None:
        (self.local_path / LocalState.LOCKED).unlink(missing_ok=True)
        (self.local_path / LocalState.SYNCING).unlink(missing_ok=True)

    def set_permissions(self, writable: bool) -> None:
        if not self.data_path.exists():
            raise LockSyncError(f"Local data path {self.data_path} does not exist.")

        mode = "u+w" if writable else "a-w"
        try:
            subprocess.run(["chmod", "-R", mode, str(self.data_path)], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise LockSyncError(f"Failed to change permissions: {e.stderr}")


class RemoteClient:
    def __init__(self, config: Config, local_data_path: Path) -> None:
        self.config = config
        self.local_data_path = local_data_path
        self.remote_data_path = f"{config.remote_dir}/data"
        self.remote_lock_path = f"{config.remote_dir}/lock"
        self.remote_info_path = f"{self.remote_lock_path}/info.txt"

    def run_ssh(self, cmd: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["ssh", '-q', self.config.remote_server, cmd], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise LockSyncError(f"SSH command failed.\nCommand: {cmd}\nStdout: {e.stdout}\nStderr: {e.stderr}")

    def run_ssh_nocheck(self, cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["ssh", '-q', self.config.remote_server, cmd], capture_output=True, text=True)

    def write_lock_info(self, local_lock_file: Path) -> None:
        try:
            subprocess.run(["scp", '-q', f"{local_lock_file}", f"{self.config.remote_server}:{shlex.quote(self.remote_info_path)}"], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise LockSyncError(f"SCP error when trying to write lock info to {self.remote_info_path}.\nStdout: {e.stdout}\nStderr: {e.stderr}")

    def run_rsync(self, direction: Direction) -> None:
        # Trailing slashes are critical in rsync to sync directory contents rather than the directory itself.
        local = f"{self.local_data_path}/"
        remote = f"{self.config.remote_server}:{self.remote_data_path}/"

        cmd = ["rsync", "-avz", "-s", "--delete"]
        if direction == Direction.UP:
            cmd.extend([local, remote])
        else:
            cmd.extend([remote, local])

        print(f"Running rsync ({direction})...")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise LockSyncError(f"Rsync failed.\nStdout: {e.stdout}\nStderr: {e.stderr}")

    def remote_dir_exists(self) -> bool:
        res = self.run_ssh_nocheck(f"[ -d {shlex.quote(self.config.remote_dir)} ]")
        return res.returncode == 0

    def initialize_remote(self, force: bool) -> None:
        if force:
            self.run_ssh(f"mkdir -p {shlex.quote(self.config.remote_dir)} {shlex.quote(self.remote_data_path)}")
        else:
            # Without -p, this inherently fails if the directory already exists or parents are missing
            self.run_ssh(f"mkdir {shlex.quote(self.config.remote_dir)} {shlex.quote(self.remote_data_path)}")

@dataclass
class LockContext:
    manager: 'LockManager'
    lock_type: LockType

    def __enter__(self) -> None:
        pass

    def __exit__(self, *args: Any) -> None:
        self.manager.release(self.lock_type)

class LockManager:
    def __init__(self, local_mgr: LocalFolderManager, remote_client: RemoteClient) -> None:
        self.local_mgr = local_mgr
        self.remote = remote_client

    def _get_local_ip(self) -> str:
        # Ask the SSH server what IP it sees us connecting from
        res = self.remote.run_ssh_nocheck("echo $SSH_CLIENT")
        parts = res.stdout.split()
        if parts:
            return parts[0]
        return "unknown"

    def _generate_info(self, lock_type: LockType) -> Dict[str, Any]:
        return {
            "lock_type": lock_type,
            "hostname": socket.gethostname(),
            "ip": self._get_local_ip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def acquire(self, lock_type: LockType, force: bool = False) -> LockContext:
        current_state = self.local_mgr.get_current_state()
        if current_state != LocalState.NONE and not force:
            raise LockSyncError(f"Local state indicates lock exists ({current_state}). Use --force to override.")

        if force:
            self.remote.run_ssh_nocheck(f"rm -rf {shlex.quote(self.remote.remote_lock_path)}")

        # Atomic lock via mkdir
        res = self.remote.run_ssh_nocheck(f"mkdir {shlex.quote(self.remote.remote_lock_path)}")
        if res.returncode != 0:
            info_res = self.remote.run_ssh_nocheck(f"cat {shlex.quote(self.remote.remote_info_path)}")
            msg = "Remote lock already held!"
            if info_res.returncode == 0:
                msg += f"\n\n--- Remote Lock Info ---\n{info_res.stdout}\n------------------------"
            else:
                msg += "\nCould not read remote info.txt (might be transitioning)."
            raise LockSyncError(msg)

        info = self._generate_info(lock_type)
        target_state = LocalState.LOCKED if lock_type == LockType.RW else LocalState.SYNCING
        target_file = self.local_mgr.write_state_file(target_state, info)

        try:
            self.remote.write_lock_info(target_file)
        except LockSyncError:
            self.local_mgr.clear_state_files()
            self.remote.run_ssh_nocheck(f"rm -rf {shlex.quote(self.remote.remote_lock_path)}")
            raise

        return LockContext(self, lock_type)

    def upgrade(self) -> None:
        self.verify_lock(LockType.RO)

        info = self._generate_info(LockType.RW)
        target_file = self.local_mgr.write_state_file(LocalState.LOCKED, info)

        try:
            self.remote.write_lock_info(target_file)
        except LockSyncError:
            self.local_mgr.clear_state_files()
            raise

    def verify_lock(self, lock_type: LockType) -> None:
        current_state = self.local_mgr.get_current_state()

        if lock_type == LockType.RW and current_state == LocalState.LOCKED:
            pass
        elif lock_type == LockType.RO and current_state == LocalState.SYNCING:
            pass
        else:
            raise LockSyncError(f"Local state is {current_state}, unexpected for lock type {lock_type}.")

        local_info = self.local_mgr.read_state_file(current_state)
        info_res = self.remote.run_ssh_nocheck(f"cat {shlex.quote(self.remote.remote_info_path)}")

        if info_res.returncode != 0:
            raise LockSyncError("Could not read remote lock info. The remote lock may have been broken by another user.")

        try:
            remote_info = json.loads(info_res.stdout)
        except json.JSONDecodeError:
            raise LockSyncError("Remote info.txt is corrupt or invalid JSON.")

        if local_info != remote_info:
            raise LockSyncError("Remote lock info does not match local info. Lock was likely broken and re-acquired by another user.")

    def release(self, lock_type: LockType) -> None:
        try:
            self.verify_lock(lock_type)
            self.remote.run_ssh_nocheck(f"rm -rf {shlex.quote(self.remote.remote_lock_path)}")
        except LockSyncError as e:
            print(f"\n[Warning] {e} Leaving remote lock intact.", file=sys.stderr)

        # Always enforce local read-only state and clear local lock files,
        # even if the remote lock was lost, to ensure local integrity.
        self.local_mgr.set_permissions(writable=False)
        self.local_mgr.clear_state_files()


# --- CLI Commands ---

def cmd_create(args: argparse.Namespace) -> None:
    local_path = Path(args.local_folder).resolve()
    conf_mgr = ConfigManager(local_path)
    local_mgr = LocalFolderManager(local_path)

    local_mgr.initialize(args.force)
    conf = conf_mgr.create(args.remote_server, args.remote_dir, args.force)
    remote = RemoteClient(conf, local_mgr.data_path)

    if remote.remote_dir_exists() and not args.force:
        raise LockSyncError(f"Remote directory {args.remote_dir} already exists. Use --force to override.")

    remote.initialize_remote(args.force)
    local_mgr.set_permissions(writable=False)
    print("Successfully created remote sync folder and local configuration.")


def cmd_join(args: argparse.Namespace) -> None:
    local_path = Path(args.local_folder).resolve()
    conf_mgr = ConfigManager(local_path)
    local_mgr = LocalFolderManager(local_path)

    local_mgr.initialize(args.force)
    conf = conf_mgr.create(args.remote_server, args.remote_dir, args.force)
    remote = RemoteClient(conf, local_mgr.data_path)

    if not remote.remote_dir_exists():
        raise LockSyncError(f"Remote directory {args.remote_dir} does not exist.")

    local_mgr.set_permissions(writable=False)
    lock_mgr = LockManager(local_mgr, remote)

    print("Fetching initial data...")
    with lock_mgr.acquire(LockType.RO, args.force):
        local_mgr.set_permissions(writable=True)
        remote.run_rsync(Direction.DOWN)

    print("Successfully joined and downloaded sync folder.")


def cmd_down(args: argparse.Namespace) -> None:
    local_path = Path(args.local_folder).resolve()
    conf_mgr = ConfigManager(local_path)
    conf = conf_mgr.load()

    local_mgr = LocalFolderManager(local_path)
    remote = RemoteClient(conf, local_mgr.data_path)
    lock_mgr = LockManager(local_mgr, remote)

    with lock_mgr.acquire(LockType.RO, args.force):
        local_mgr.set_permissions(writable=True)
        remote.run_rsync(Direction.DOWN)
    print("Successfully downloaded latest data.")


def cmd_lock(args: argparse.Namespace) -> None:
    local_path = Path(args.local_folder).resolve()
    conf_mgr = ConfigManager(local_path)
    conf = conf_mgr.load()

    local_mgr = LocalFolderManager(local_path)
    remote = RemoteClient(conf, local_mgr.data_path)
    lock_mgr = LockManager(local_mgr, remote)

    lock_mgr.acquire(LockType.RO, args.force)
    local_mgr.set_permissions(writable=True)

    try:
        remote.run_rsync(Direction.DOWN)
    except BaseException:
        # If rsync fails during lock acquisition, halt in an intermediate state
        lock_mgr.release(LockType.RO)
        raise

    lock_mgr.upgrade()
    print("Successfully locked for editing. You may now modify files in data/.")


def cmd_up(args: argparse.Namespace) -> None:
    local_path = Path(args.local_folder).resolve()
    conf_mgr = ConfigManager(local_path)
    conf = conf_mgr.load()

    local_mgr = LocalFolderManager(local_path)
    remote = RemoteClient(conf, local_mgr.data_path)
    lock_mgr = LockManager(local_mgr, remote)

    lock_mgr.verify_lock(LockType.RW)
    remote.run_rsync(Direction.UP)

    # We only release if rsync succeeds. If it fails, they still own the lock and can retry.
    lock_mgr.release(LockType.RW)
    print("Successfully uploaded changes and released lock.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict lock-based folder synchronization utility.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Base parser for common arguments
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("local_folder", nargs='?', default=None, help="Path to local synchronization folder")
    parent_parser.add_argument("-f", "--force", action="store_true", help="Force operation / break locks")

    # Command: create
    parser_create = subparsers.add_parser("create", parents=[parent_parser], help="Create a new sync folder on remote")
    parser_create.add_argument("remote_server", help="SSH remote server (e.g. user@host)")
    parser_create.add_argument("remote_dir", help="Path to remote synchronization directory")

    # Command: join
    parser_join = subparsers.add_parser("join", parents=[parent_parser], help="Join an existing remote sync folder")
    parser_join.add_argument("remote_server", help="SSH remote server (e.g. user@host)")
    parser_join.add_argument("remote_dir", help="Path to remote synchronization directory")

    # Command: down
    subparsers.add_parser("down", parents=[parent_parser], help="Sync remote changes locally without locking")

    # Command: lock
    subparsers.add_parser("lock", parents=[parent_parser], help="Sync locally and acquire read-write lock")

    # Command: up
    subparsers.add_parser("up", parents=[parent_parser], help="Upload local changes to remote and release lock")

    args = parser.parse_args()
    if args.local_folder is None:
        args.local_folder = guess_local_folder()

    try:
        if args.command == "create":
            cmd_create(args)
        elif args.command == "join":
            cmd_join(args)
        elif args.command == "down":
            cmd_down(args)
        elif args.command == "lock":
            cmd_lock(args)
        elif args.command == "up":
            cmd_up(args)
    except LockSyncError as e:
        print(f"\n[Error] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[Cancelled] Operation interrupted by user.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
