"""Ansible runtime environment manager."""
from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, no_type_check

import subprocess_tee
from packaging.version import Version

from ansible_compat.config import (
    AnsibleConfig,
    ansible_collections_path,
    ansible_version,
    parse_ansible_version,
)
from ansible_compat.constants import (
    META_MAIN,
    MSG_INVALID_FQRL,
    RC_ANSIBLE_OPTIONS_ERROR,
    REQUIREMENT_LOCATIONS,
)
from ansible_compat.errors import (
    AnsibleCommandError,
    AnsibleCompatError,
    InvalidPrerequisiteError,
    MissingAnsibleError,
)
from ansible_compat.loaders import colpath_from_path, yaml_from_file
from ansible_compat.prerun import get_cache_dir

if TYPE_CHECKING:
    # https://github.com/PyCQA/pylint/issues/3240
    # pylint: disable=unsubscriptable-object
    CompletedProcess = subprocess.CompletedProcess[Any]
else:
    CompletedProcess = subprocess.CompletedProcess


_logger = logging.getLogger(__name__)
# regex to extract the first version from a collection range specifier
version_re = re.compile(":[>=<]*([^,]*)")
namespace_re = re.compile("^[a-z][a-z0-9_]+$")


class AnsibleWarning(Warning):
    """Warnings related to Ansible runtime."""


@dataclass
class Collection:
    """Container for Ansible collection information."""

    name: str
    version: str
    path: Path


class CollectionVersion(Version):
    """Collection version."""

    def __init__(self, version: str) -> None:
        """Initialize collection version."""
        # As packaging Version class does not support wildcard, we convert it
        # to "0", as this being the smallest version possible.
        if version == "*":
            version = "0"
        super().__init__(version)


@dataclass
class Plugins:  # pylint: disable=too-many-instance-attributes
    """Dataclass to access installed Ansible plugins, uses ansible-doc to retrieve them."""

    runtime: Runtime
    become: dict[str, str] = field(init=False)
    cache: dict[str, str] = field(init=False)
    callback: dict[str, str] = field(init=False)
    cliconf: dict[str, str] = field(init=False)
    connection: dict[str, str] = field(init=False)
    httpapi: dict[str, str] = field(init=False)
    inventory: dict[str, str] = field(init=False)
    lookup: dict[str, str] = field(init=False)
    netconf: dict[str, str] = field(init=False)
    shell: dict[str, str] = field(init=False)
    vars: dict[str, str] = field(init=False)  # noqa: A003
    module: dict[str, str] = field(init=False)
    strategy: dict[str, str] = field(init=False)
    test: dict[str, str] = field(init=False)
    filter: dict[str, str] = field(init=False)  # noqa: A003
    role: dict[str, str] = field(init=False)
    keyword: dict[str, str] = field(init=False)

    @no_type_check
    def __getattribute__(self, attr: str):  # noqa: ANN204
        """Get attribute."""
        if attr in {
            "become",
            "cache",
            "callback",
            "cliconf",
            "connection",
            "httpapi",
            "inventory",
            "lookup",
            "netconf",
            "shell",
            "vars",
            "module",
            "strategy",
            "test",
            "filter",
            "role",
            "keyword",
        }:
            try:
                result = super().__getattribute__(attr)
            except AttributeError as exc:
                if ansible_version() < Version("2.14") and attr in {"filter", "test"}:
                    msg = "Ansible version below 2.14 does not support retrieving filter and test plugins."
                    raise RuntimeError(msg) from exc
                proc = self.runtime.run(
                    ["ansible-doc", "--json", "-l", "-t", attr],
                )
                data = json.loads(proc.stdout)
                if not isinstance(data, dict):  # pragma: no cover
                    msg = "Unexpected output from ansible-doc"
                    raise AnsibleCompatError(msg) from exc
                result = data
        else:
            result = super().__getattribute__(attr)

        return result


# pylint: disable=too-many-instance-attributes
class Runtime:
    """Ansible Runtime manager."""

    _version: Version | None = None
    collections: OrderedDict[str, Collection] = OrderedDict()
    cache_dir: Path | None = None
    # Used to track if we have already initialized the Ansible runtime as attempts
    # to do it multiple tilmes will cause runtime warnings from within ansible-core
    initialized: bool = False
    plugins: Plugins

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        isolated: bool = False,
        min_required_version: str | None = None,
        require_module: bool = False,
        max_retries: int = 0,
        environ: dict[str, str] | None = None,
    ) -> None:
        """Initialize Ansible runtime environment.

        :param project_dir: The directory containing the Ansible project. If
                            not mentioned it will be guessed from the current
                            working directory.
        :param isolated: Assure that installation of collections or roles
                         does not affect Ansible installation, an unique cache
                         directory being used instead.
        :param min_required_version: Minimal version of Ansible required. If
                                     not found, a :class:`RuntimeError`
                                     exception is raised.
        :param require_module: If set, instantiation will fail if Ansible
                               Python module is missing or is not matching
                               the same version as the Ansible command line.
                               That is useful for consumers that expect to
                               also perform Python imports from Ansible.
        :param max_retries: Number of times it should retry network operations.
                            Default is 0, no retries.
        :param environ: Environment dictionary to use, if undefined
                        ``os.environ`` will be copied and used.
        """
        self.project_dir = project_dir or Path.cwd()
        self.isolated = isolated
        self.max_retries = max_retries
        self.environ = environ or os.environ.copy()
        self.plugins = Plugins(runtime=self)
        # Reduce noise from paramiko, unless user already defined PYTHONWARNINGS
        # paramiko/transport.py:236: CryptographyDeprecationWarning: Blowfish has been deprecated
        # https://github.com/paramiko/paramiko/issues/2038
        # As CryptographyDeprecationWarning is not a builtin, we cannot use
        # PYTHONWARNINGS to ignore it using category but we can use message.
        # https://stackoverflow.com/q/68251969/99834
        if "PYTHONWARNINGS" not in self.environ:  # pragma: no cover
            self.environ["PYTHONWARNINGS"] = "ignore:Blowfish has been deprecated"

        if isolated:
            self.cache_dir = get_cache_dir(self.project_dir)
        self.config = AnsibleConfig()

        # Add the sys.path to the collection paths if not isolated
        self._add_sys_path_to_collection_paths()

        if not self.version_in_range(lower=min_required_version):
            msg = f"Found incompatible version of ansible runtime {self.version}, instead of {min_required_version} or newer."
            raise RuntimeError(msg)
        if require_module:
            self._ensure_module_available()

        # pylint: disable=import-outside-toplevel
        from ansible.utils.display import Display

        # pylint: disable=unused-argument
        def warning(
            self: Display,  # noqa: ARG001
            msg: str,
            *,
            formatted: bool = False,  # noqa: ARG001
        ) -> None:
            """Override ansible.utils.display.Display.warning to avoid printing warnings."""
            warnings.warn(
                message=msg,
                category=AnsibleWarning,
                stacklevel=2,
                source={"msg": msg},
            )

        # Monkey patch ansible warning in order to use warnings module.
        Display.warning = warning

    def _add_sys_path_to_collection_paths(self) -> None:
        """Add the sys.path to the collection paths."""
        if self.config.collections_scan_sys_path:
            for path in sys.path:
                if (
                    path not in self.config.collections_paths
                    and (Path(path) / "ansible_collections").is_dir()
                ):
                    self.config.collections_paths.append(  # pylint: disable=E1101
                        path,
                    )

    def load_collections(self) -> None:
        """Load collection data."""
        self.collections = OrderedDict()
        no_collections_msg = "None of the provided paths were usable"

        proc = self.run(["ansible-galaxy", "collection", "list", "--format=json"])
        if proc.returncode == RC_ANSIBLE_OPTIONS_ERROR and (
            no_collections_msg in proc.stdout or no_collections_msg in proc.stderr
        ):
            _logger.debug("Ansible reported no installed collections at all.")
            return
        if proc.returncode != 0:
            _logger.error(proc)
            msg = f"Unable to list collections: {proc}"
            raise RuntimeError(msg)
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            msg = f"Unexpected collection data, {data}"
            raise TypeError(msg)
        for path in data:
            for collection, collection_info in data[path].items():
                if not isinstance(collection, str):
                    msg = f"Unexpected collection data, {collection}"
                    raise TypeError(msg)
                if not isinstance(collection_info, dict):
                    msg = f"Unexpected collection data, {collection_info}"
                    raise TypeError(msg)

                self.collections[collection] = Collection(
                    name=collection,
                    version=collection_info["version"],
                    path=path,
                )

    def _ensure_module_available(self) -> None:
        """Assure that Ansible Python module is installed and matching CLI version."""
        ansible_release_module = None
        with contextlib.suppress(ModuleNotFoundError, ImportError):
            ansible_release_module = importlib.import_module("ansible.release")

        if ansible_release_module is None:
            msg = "Unable to find Ansible python module."
            raise RuntimeError(msg)

        ansible_module_version = Version(
            ansible_release_module.__version__,
        )
        if ansible_module_version != self.version:
            msg = f"Ansible CLI ({self.version}) and python module ({ansible_module_version}) versions do not match. This indicates a broken execution environment."
            raise RuntimeError(msg)

        # For ansible 2.15+ we need to initialize the plugin loader
        # https://github.com/ansible/ansible-lint/issues/2945
        if not Runtime.initialized:
            col_path = [f"{self.cache_dir}/collections"]
            if self.version >= Version("2.15.0.dev0"):
                # pylint: disable=import-outside-toplevel,no-name-in-module
                from ansible.plugins.loader import init_plugin_loader

                init_plugin_loader(col_path)
            else:
                # noinspection PyProtectedMember
                from ansible.utils.collection_loader._collection_finder import (  # pylint: disable=import-outside-toplevel
                    _AnsibleCollectionFinder,
                )

                # noinspection PyProtectedMember
                # pylint: disable=protected-access
                col_path += self.config.collections_paths
                col_path += os.path.dirname(  # noqa: PTH120
                    os.environ.get(ansible_collections_path(), "."),
                ).split(":")
                _AnsibleCollectionFinder(  # noqa: SLF001
                    paths=col_path,
                )._install()  # pylint: disable=protected-access
            Runtime.initialized = True

    def clean(self) -> None:
        """Remove content of cache_dir."""
        if self.cache_dir:
            shutil.rmtree(self.cache_dir, ignore_errors=True)

    def run(  # ruff: disable=PLR0913
        self,
        args: str | list[str],
        *,
        retry: bool = False,
        tee: bool = False,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CompletedProcess:
        """Execute a command inside an Ansible environment.

        :param retry: Retry network operations on failures.
        :param tee: Also pass captured stdout/stderr to system while running.
        """
        if tee:
            run_func: Callable[..., CompletedProcess] = subprocess_tee.run
        else:
            run_func = subprocess.run
        env = self.environ if env is None else env.copy()
        # Presence of ansible debug variable or config option will prevent us
        # from parsing its JSON output due to extra debug messages on stdout.
        env["ANSIBLE_DEBUG"] = "0"

        # https://github.com/ansible/ansible-lint/issues/3522
        env["ANSIBLE_VERBOSE_TO_STDERR"] = "True"

        for _ in range(self.max_retries + 1 if retry else 1):
            result = run_func(
                args,
                universal_newlines=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(cwd) if cwd else None,
            )
            if result.returncode == 0:
                break
            _logger.debug("Environment: %s", env)
            if retry:
                _logger.warning(
                    "Retrying execution failure %s of: %s",
                    result.returncode,
                    " ".join(args),
                )
        return result

    @property
    def version(self) -> Version:
        """Return current Version object for Ansible.

        If version is not mentioned, it returns current version as detected.
        When version argument is mentioned, it return converts the version string
        to Version object in order to make it usable in comparisons.
        """
        if self._version:
            return self._version

        proc = self.run(["ansible", "--version"])
        if proc.returncode == 0:
            self._version = parse_ansible_version(proc.stdout)
            return self._version

        msg = "Unable to find a working copy of ansible executable."
        raise MissingAnsibleError(msg, proc=proc)

    def version_in_range(
        self,
        lower: str | None = None,
        upper: str | None = None,
    ) -> bool:
        """Check if Ansible version is inside a required range.

        The lower limit is inclusive and the upper one exclusive.
        """
        if lower and self.version < Version(lower):
            return False
        if upper and self.version >= Version(upper):
            return False
        return True

    def install_collection(
        self,
        collection: str | Path,
        *,
        destination: Path | None = None,
        force: bool = False,
    ) -> None:
        """Install an Ansible collection.

        Can accept arguments like:
            'foo.bar:>=1.2.3'
            'git+https://github.com/ansible-collections/ansible.posix.git,main'
        """
        cmd = [
            "ansible-galaxy",
            "collection",
            "install",
            "-vvv",  # this is needed to make ansible display important info in case of failures
        ]
        if force:
            cmd.append("--force")

        if isinstance(collection, Path):
            collection = str(collection)
        # As ansible-galaxy install is not able to automatically determine
        # if the range requires a pre-release, we need to manuall add the --pre
        # flag when needed.
        matches = version_re.search(collection)

        if (
            not is_url(collection)
            and matches
            and CollectionVersion(matches[1]).is_prerelease
        ):
            cmd.append("--pre")

        cpaths: list[str] = self.config.collections_paths
        if destination and str(destination) not in cpaths:
            # we cannot use '-p' because it breaks galaxy ability to ignore already installed collections, so
            # we hack ansible_collections_path instead and inject our own path there.
            # pylint: disable=no-member
            cpaths.insert(0, str(destination))
        cmd.append(f"{collection}")

        _logger.info("Running from %s : %s", Path.cwd(), " ".join(cmd))
        run = self.run(
            cmd,
            retry=True,
            env={**self.environ, ansible_collections_path(): ":".join(cpaths)},
        )
        if run.returncode != 0:
            msg = f"Command returned {run.returncode} code:\n{run.stdout}\n{run.stderr}"
            _logger.error(msg)
            raise InvalidPrerequisiteError(msg)

    def install_collection_from_disk(
        self,
        path: Path,
        destination: Path | None = None,
    ) -> None:
        """Build and install collection from a given disk path."""
        if not self.version_in_range(upper="2.11"):
            self.install_collection(path, destination=destination, force=True)
            return
        # older versions of ansible able unable to install without building
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmd = [
                "ansible-galaxy",
                "collection",
                "build",
                "--output-path",
                str(tmp_dir),
                str(path),
            ]
            _logger.info("Running %s", " ".join(cmd))
            run = self.run(cmd, retry=False)
            if run.returncode != 0:
                _logger.error(run.stdout)
                raise AnsibleCommandError(run)
            for archive_file in os.listdir(tmp_dir):
                self.install_collection(
                    str(Path(tmp_dir) / archive_file),
                    destination=destination,
                    force=True,
                )

    # pylint: disable=too-many-branches
    def install_requirements(  # noqa: C901
        self,
        requirement: Path,
        *,
        retry: bool = False,
        offline: bool = False,
    ) -> None:
        """Install dependencies from a requirements.yml.

        :param requirement: path to requirements.yml file
        :param retry: retry network operations on failures
        :param offline: bypass installation, may fail if requirements are not met.
        """
        if not Path(requirement).exists():
            return
        reqs_yaml = yaml_from_file(Path(requirement))
        if not isinstance(reqs_yaml, (dict, list)):
            msg = f"{requirement} file is not a valid Ansible requirements file."
            raise InvalidPrerequisiteError(msg)

        if isinstance(reqs_yaml, dict):
            for key in reqs_yaml:
                if key not in ("roles", "collections"):
                    msg = f"{requirement} file is not a valid Ansible requirements file. Only 'roles' and 'collections' keys are allowed at root level. Recognized valid locations are: {', '.join(REQUIREMENT_LOCATIONS)}"
                    raise InvalidPrerequisiteError(msg)

        if isinstance(reqs_yaml, list) or "roles" in reqs_yaml:
            cmd = [
                "ansible-galaxy",
                "role",
                "install",
                "-vr",
                f"{requirement}",
            ]
            if self.cache_dir:
                cmd.extend(["--roles-path", f"{self.cache_dir}/roles"])

            if offline:
                _logger.warning(
                    "Skipped installing old role dependencies due to running in offline mode.",
                )
            else:
                _logger.info("Running %s", " ".join(cmd))

                result = self.run(cmd, retry=retry)
                if result.returncode != 0:
                    _logger.error(result.stdout)
                    raise AnsibleCommandError(result)

        # Run galaxy collection install works on v2 requirements.yml
        if "collections" in reqs_yaml and reqs_yaml["collections"] is not None:
            cmd = [
                "ansible-galaxy",
                "collection",
                "install",
                "-v",
            ]
            for collection in reqs_yaml["collections"]:
                if isinstance(collection, dict) and collection.get("type", "") == "git":
                    _logger.info(
                        "Adding '--pre' to ansible-galaxy collection install because we detected one collection being sourced from git.",
                    )
                    cmd.append("--pre")
                    break
            if offline:
                _logger.warning(
                    "Skipped installing collection dependencies due to running in offline mode.",
                )
            else:
                cmd.extend(["-r", str(requirement)])
                cpaths = self.config.collections_paths
                if self.cache_dir:
                    # we cannot use '-p' because it breaks galaxy ability to ignore already installed collections, so
                    # we hack ansible_collections_path instead and inject our own path there.
                    dest_path = f"{self.cache_dir}/collections"
                    if dest_path not in cpaths:
                        # pylint: disable=no-member
                        cpaths.insert(0, dest_path)
                _logger.info("Running %s", " ".join(cmd))
                result = self.run(
                    cmd,
                    retry=retry,
                    env={**os.environ, "ANSIBLE_COLLECTIONS_PATH": ":".join(cpaths)},
                )
                if result.returncode != 0:
                    _logger.error(result.stdout)
                    _logger.error(result.stderr)
                    raise AnsibleCommandError(result)

    def prepare_environment(  # noqa: C901
        self,
        required_collections: dict[str, str] | None = None,
        *,
        retry: bool = False,
        install_local: bool = False,
        offline: bool = False,
        role_name_check: int = 0,
    ) -> None:
        """Make dependencies available if needed."""
        destination: Path | None = None
        if required_collections is None:
            required_collections = {}

        # first one is standard for collection layout repos and the last two
        # are part of Tower specification
        # https://docs.ansible.com/ansible-tower/latest/html/userguide/projects.html#ansible-galaxy-support
        # https://docs.ansible.com/ansible-tower/latest/html/userguide/projects.html#collections-support
        for req_file in REQUIREMENT_LOCATIONS:
            self.install_requirements(Path(req_file), retry=retry, offline=offline)

        self._prepare_ansible_paths()

        if not install_local:
            return

        for gpath in search_galaxy_paths(self.project_dir):
            # processing all found galaxy.yml files
            galaxy_path = Path(gpath)
            if galaxy_path.exists():
                data = yaml_from_file(galaxy_path)
                if isinstance(data, dict) and "dependencies" in data:
                    for name, required_version in data["dependencies"].items():
                        _logger.info(
                            "Provisioning collection %s:%s from galaxy.yml",
                            name,
                            required_version,
                        )
                        self.install_collection(
                            f"{name}{',' if is_url(name) else ':'}{required_version}",
                            destination=destination,
                        )

        if self.cache_dir:
            destination = self.cache_dir / "collections"
        for name, min_version in required_collections.items():
            self.install_collection(
                f"{name}:>={min_version}",
                destination=destination,
            )

        if Path("galaxy.yml").exists():
            if destination:
                # while function can return None, that would not break the logic
                colpath = Path(
                    f"{destination}/ansible_collections/{colpath_from_path(Path.cwd())}",
                )
                if colpath.is_symlink():
                    if os.path.realpath(colpath) == Path.cwd():
                        _logger.warning(
                            "Found symlinked collection, skipping its installation.",
                        )
                        return
                    _logger.warning(
                        "Collection is symlinked, but not pointing to %s directory, so we will remove it.",
                        Path.cwd(),
                    )
                    colpath.unlink()

            # molecule scenario within a collection
            self.install_collection_from_disk(
                galaxy_path.parent,
                destination=destination,
            )
        elif (
            Path().resolve().parent.name == "roles"
            and Path("../../galaxy.yml").exists()
        ):
            # molecule scenario located within roles/<role-name>/molecule inside
            # a collection
            self.install_collection_from_disk(
                Path("../.."),
                destination=destination,
            )
        else:
            # no collection, try to recognize and install a standalone role
            self._install_galaxy_role(
                self.project_dir,
                role_name_check=role_name_check,
                ignore_errors=True,
            )
        # reload collections
        self.load_collections()

    def require_collection(
        self,
        name: str,
        version: str | None = None,
        *,
        install: bool = True,
    ) -> tuple[CollectionVersion, Path]:
        """Check if a minimal collection version is present or exits.

        In the future this method may attempt to install a missing or outdated
        collection before failing.

        :param name: collection name
        :param version: minimal version required
        :param install: if True, attempt to install a missing collection
        :returns: tuple of (found_version, collection_path)
        """
        try:
            ns, coll = name.split(".", 1)
        except ValueError as exc:
            msg = f"Invalid collection name supplied: {name}%s"
            raise InvalidPrerequisiteError(
                msg,
            ) from exc

        paths: list[str] = self.config.collections_paths
        if not paths or not isinstance(paths, list):
            msg = f"Unable to determine ansible collection paths. ({paths})"
            raise InvalidPrerequisiteError(
                msg,
            )

        if self.cache_dir:
            # if we have a cache dir, we want to be use that would be preferred
            # destination when installing a missing collection
            # https://github.com/PyCQA/pylint/issues/4667
            paths.insert(0, f"{self.cache_dir}/collections")  # pylint: disable=E1101

        for path in paths:
            collpath = Path(path) / "ansible_collections" / ns / coll
            if collpath.exists():
                mpath = collpath / "MANIFEST.json"
                if not mpath.exists():
                    msg = f"Found collection at '{collpath}' but missing MANIFEST.json, cannot get info."
                    _logger.fatal(msg)
                    raise InvalidPrerequisiteError(msg)

                with mpath.open(encoding="utf-8") as f:
                    manifest = json.loads(f.read())
                    found_version = CollectionVersion(
                        manifest["collection_info"]["version"],
                    )
                    if version and found_version < CollectionVersion(version):
                        if install:
                            self.install_collection(f"{name}:>={version}")
                            self.require_collection(name, version, install=False)
                        else:
                            msg = f"Found {name} collection {found_version} but {version} or newer is required."
                            _logger.fatal(msg)
                            raise InvalidPrerequisiteError(msg)
                    return found_version, collpath.resolve()
                break
        else:
            if install:
                self.install_collection(f"{name}:>={version}" if version else name)
                return self.require_collection(
                    name=name,
                    version=version,
                    install=False,
                )
            msg = f"Collection '{name}' not found in '{paths}'"
            _logger.fatal(msg)
            raise InvalidPrerequisiteError(msg)

    def _prepare_ansible_paths(self) -> None:
        """Configure Ansible environment variables."""
        try:
            library_paths: list[str] = self.config.default_module_path.copy()
            roles_path: list[str] = self.config.default_roles_path.copy()
            collections_path: list[str] = self.config.collections_paths.copy()
        except AttributeError as exc:
            msg = "Unexpected ansible configuration"
            raise RuntimeError(msg) from exc

        alterations_list = [
            (library_paths, "plugins/modules", True),
            (roles_path, "roles", True),
        ]

        alterations_list.extend(
            [
                (roles_path, f"{self.cache_dir}/roles", False),
                (library_paths, f"{self.cache_dir}/modules", False),
                (collections_path, f"{self.cache_dir}/collections", False),
            ]
            if self.isolated
            else [],
        )

        for path_list, path_, must_be_present in alterations_list:
            path = Path(path_)
            if not path.exists():
                if must_be_present:
                    continue
                path.mkdir(parents=True, exist_ok=True)
            if path not in path_list:
                path_list.insert(0, str(path))

        if library_paths != self.config.DEFAULT_MODULE_PATH:
            self._update_env("ANSIBLE_LIBRARY", library_paths)
        if collections_path != self.config.collections_paths:
            self._update_env(ansible_collections_path(), collections_path)
        if roles_path != self.config.default_roles_path:
            self._update_env("ANSIBLE_ROLES_PATH", roles_path)

    def _get_roles_path(self) -> Path:
        """Return roles installation path.

        If `self.isolated` is set to `True`, `self.cache_dir` would be
        created, then it returns the `self.cache_dir/roles`. When `self.isolated` is
        not mentioned or set to `False`, it returns the first path in
        `default_roles_path`.
        """
        if self.cache_dir:
            path = Path(f"{self.cache_dir}/roles")
        else:
            path = Path(self.config.default_roles_path[0]).expanduser()
        return path

    def _install_galaxy_role(
        self,
        project_dir: Path,
        role_name_check: int = 0,
        *,
        ignore_errors: bool = False,
    ) -> None:
        """Detect standalone galaxy role and installs it.

        :param: role_name_check: logic to used to check role name
            0: exit with error if name is not compliant (default)
            1: warn if name is not compliant
            2: bypass any name checking

        :param: ignore_errors: if True, bypass installing invalid roles.

        Our implementation aims to match ansible-galaxy's behaviour for installing
        roles from a tarball or scm. For example ansible-galaxy will install a role
        that has both galaxy.yml and meta/main.yml present but empty. Also missing
        galaxy.yml is accepted but missing meta/main.yml is not.
        """
        yaml = None
        galaxy_info = {}

        for meta_main in META_MAIN:
            meta_filename = Path(project_dir) / meta_main

            if meta_filename.exists():
                break
        else:
            if ignore_errors:
                return

        yaml = yaml_from_file(meta_filename)

        if yaml and "galaxy_info" in yaml:
            galaxy_info = yaml["galaxy_info"]

        fqrn = _get_role_fqrn(galaxy_info, project_dir)

        if role_name_check in [0, 1]:
            if not re.match(r"[a-z0-9][a-z0-9_]+\.[a-z][a-z0-9_]+$", fqrn):
                msg = MSG_INVALID_FQRL.format(fqrn)
                if role_name_check == 1:
                    _logger.warning(msg)
                else:
                    _logger.error(msg)
                    raise InvalidPrerequisiteError(msg)
        elif "role_name" in galaxy_info:
            # when 'role-name' is in skip_list, we stick to plain role names
            role_namespace = _get_galaxy_role_ns(galaxy_info)
            role_name = _get_galaxy_role_name(galaxy_info)
            fqrn = f"{role_namespace}{role_name}"
        else:
            fqrn = Path(project_dir).absolute().name
        path = self._get_roles_path()
        path.mkdir(parents=True, exist_ok=True)
        link_path = path / fqrn
        # despite documentation stating that is_file() reports true for symlinks,
        # it appears that is_dir() reports true instead, so we rely on exists().
        target = Path(project_dir).absolute()
        if not link_path.exists() or (
            link_path.is_symlink() and link_path.readlink() != target
        ):
            # must call unlink before checking exists because a broken
            # link reports as not existing and we want to repair it
            link_path.unlink(missing_ok=True)
            # https://github.com/python/cpython/issues/73843
            link_path.symlink_to(str(target), target_is_directory=True)
        _logger.info(
            "Using %s symlink to current repository in order to enable Ansible to find the role using its expected full name.",
            link_path,
        )

    def _update_env(self, varname: str, value: list[str], default: str = "") -> None:
        """Update colon based environment variable if needed.

        New values are prepended to make sure they take precedence.
        """
        if not value:
            return
        orig_value = self.environ.get(varname, default)
        if orig_value:
            value = [*value, *orig_value.split(":")]
        value_str = ":".join(value)
        if value_str != self.environ.get(varname, ""):
            self.environ[varname] = value_str
            _logger.info("Set %s=%s", varname, value_str)


def _get_role_fqrn(galaxy_infos: dict[str, Any], project_dir: Path) -> str:
    """Compute role fqrn."""
    role_namespace = _get_galaxy_role_ns(galaxy_infos)
    role_name = _get_galaxy_role_name(galaxy_infos)

    if len(role_name) == 0:
        role_name = Path(project_dir).absolute().name
        role_name = re.sub(r"(ansible-|ansible-role-)", "", role_name).split(
            ".",
            maxsplit=2,
        )[-1]

    return f"{role_namespace}{role_name}"


def _get_galaxy_role_ns(galaxy_infos: dict[str, Any]) -> str:
    """Compute role namespace from meta/main.yml, including trailing dot."""
    role_namespace = galaxy_infos.get("namespace", "")
    if len(role_namespace) == 0:
        role_namespace = galaxy_infos.get("author", "")
    if not isinstance(role_namespace, str):
        msg = f"Role namespace must be string, not {role_namespace}"
        raise AnsibleCompatError(msg)
    # if there's a space in the name space, it's likely author name
    # and not the galaxy login, so act as if there was no namespace
    if not role_namespace or re.match(r"^\w+ \w+", role_namespace):
        role_namespace = ""
    else:
        role_namespace = f"{role_namespace}."
    return role_namespace


def _get_galaxy_role_name(galaxy_infos: dict[str, Any]) -> str:
    """Compute role name from meta/main.yml."""
    return galaxy_infos.get("role_name", "")


def search_galaxy_paths(search_dir: Path) -> list[str]:
    """Search for galaxy paths (only one level deep)."""
    galaxy_paths: list[str] = []
    for file in [".", *os.listdir(search_dir)]:
        # We ignore any folders that are not valid namespaces, just like
        # ansible galaxy does at this moment.
        if file != "." and not namespace_re.match(file):
            continue
        file_path = search_dir / file / "galaxy.yml"
        if file_path.is_file():
            galaxy_paths.append(str(file_path))
    return galaxy_paths


def is_url(name: str) -> bool:
    """Return True if a dependency name looks like an URL."""
    return bool(re.match("^git[+@]", name))
