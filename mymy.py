#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mymy.py - a script launcher that runs scripts scattered across several
directories by name, without typing full paths.

It builds one flat index of command names from every configured directory,
after which `mymy.py <name> [args...]` runs the matching script and passes
arguments and the exit code straight through.

1. Supported types
------------------
    .py                    current Python interpreter (script dir added to sys.path)
    .ps1                   PowerShell (pwsh preferred, falls back to powershell)
    .bat / .cmd            cmd.exe
    .sh                    bash (Git Bash is located automatically on Windows)
    .jar                   java -jar
    .class                 java -cp <classpath root> <fully.qualified.Name>
    .exe / .com            executed directly
    .js / .mjs / .cjs      node
    .vbs / .wsf            cscript
    other executable files executed directly (non-Windows)

2. Configuration file mymy.json
-------------------------------
Stored next to this file (mymy.py). Each entry of "directories" is either a
plain string or an object carrying filter rules:

    {
      "directories": [
        "D:/tools/scripts",
        { "path": "E:/workspace/utils" },
        { "path": "F:/Project/Java/sebook/dist",
          "filters": ["*.jar", "*.cmd", "myapp*.jar"] }
      ]
    }

Directory entry fields:

    path     required  directory to scan (dir / directory also accepted)
    filters  optional  filename glob patterns, e.g. ["*.jar", "myapp*.jar"];
                       case-insensitive, supports * ? []; omitted means every
                       supported type in the directory matches
    recurse  optional  scan subdirectories too, defaults to false
    exts     optional  restrict to these extensions, narrowing after filters

The top-level key "directories" may also be spelled "dirs".

3. Command line usage
---------------------
    python mymy.py ls            list every available script name
    python mymy.py ls -v         list names with type and full path
    python mymy.py ls --all      also show each directory's scan rules and
                                 the files excluded by filters
    python mymy.py abc arg1 arg2 run abc, passing arg1 arg2 through to it
    python mymy.py help          show this help

When files share a base name but differ in type (for example abc.py and
abc.cmd), write `mymy.py abc.py` to pick one explicitly; otherwise the file
found first is used.

4. Execution rules
------------------
    - a script is indexed by its filename without the extension;
    - when names collide, the file found first in directory order wins;
    - arguments are passed through verbatim, not re-parsed by a shell;
    - the child process runs in the current working directory and its exit
      code becomes the exit code of mymy.py;
    - a missing interpreter (java / node / bash / PowerShell) is reported
      with a clear message instead of a traceback.
"""

import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directory holding this file (and mymy.json)
BASE_DIR = Path(__file__).resolve().parent
# Path of the configuration file
CONFIG_FILE = BASE_DIR / "mymy.json"

# Executables that Windows can run directly
EXECUTABLE_EXTS = {".exe", ".com"}
# Batch files (run through cmd.exe)
BATCH_EXTS = {".bat", ".cmd"}
# PowerShell scripts
PS1_EXTS = {".ps1"}
# POSIX shell scripts
SH_EXTS = {".sh"}
# Java artifacts
JAVA_EXTS = {".jar", ".class"}
# Node.js scripts
JS_EXTS = {".js", ".mjs", ".cjs"}
# VBScript / Windows Script File (run through cscript)
VBS_EXTS = {".vbs", ".wsf"}

# Extensions that are scanned for by default
SUPPORTED_EXTS = (
    {".py"} | BATCH_EXTS | PS1_EXTS | SH_EXTS | JAVA_EXTS | EXECUTABLE_EXTS
    | JS_EXTS | VBS_EXTS
)

# Built-in subcommands
CMD_LIST = "ls"
CMD_HELP = "help"

# Message shown when a requested name cannot be found
NOT_FOUND_HINT = (
    "Script {name!r} was not found.\n"
    "Run 'python mymy.py ls -v' to list the available scripts, "
    "or check the directories and filters in {cfg}."
)


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def normalize_dir_item(item) -> dict:
    """
    Normalize one entry of the "directories" list into a dict.

    Two forms are accepted:

        "D:/scripts"
        {"path": "D:/scripts", "filters": ["*.jar"], "recurse": false, "exts": [".jar"]}

    Returns:
        dict: {"path": str, "filters": list[str], "recurse": bool,
               "exts": set[str] | None}

    Raises:
        ValueError: if the entry is malformed.
    """
    if isinstance(item, str):
        return {"path": item, "filters": [], "recurse": False, "exts": None}

    if not isinstance(item, dict):
        raise ValueError(
            f"Invalid directory entry (expected a string or an object): {item!r}"
        )

    # Field aliases: path / dir / directory are equivalent
    path = item.get("path", item.get("dir", item.get("directory")))
    if not isinstance(path, str) or not path:
        raise ValueError(f'Directory entry is missing the "path" field: {item!r}')

    filters = item.get("filters", item.get("filter", []))
    if isinstance(filters, str):
        filters = [filters]
    if not isinstance(filters, list):
        raise ValueError(
            f'"filters" must be an array of strings: {item!r}'
        )

    recurse = bool(item.get("recurse", item.get("recursive", False)))

    exts = item.get("exts")
    if isinstance(exts, str):
        exts = [exts]
    if exts is not None:
        if not isinstance(exts, list):
            raise ValueError(f'"exts" must be an array of strings: {item!r}')
        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in exts}

    return {
        "path": path,
        "filters": [str(f) for f in filters],
        "recurse": recurse,
        "exts": exts,
    }


def load_config() -> dict:
    """
    Read mymy.json, stored next to mymy.py.

    Returns:
        dict: the configuration; "directories" is normalized to a list of
              dicts (see normalize_dir_item).

    Raises:
        FileNotFoundError: if the configuration file does not exist.
        ValueError:        if the file is not valid JSON, or lacks
                           "directories".
    """
    if not CONFIG_FILE.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {CONFIG_FILE}\n"
            f'Create it, for example: {{ "directories": ["D:/scripts", '
            f'{{"path": "E:/jars", "filters": ["*.jar"]}}] }}'
        )

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as fp:
            config = json.load(fp)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Configuration file {CONFIG_FILE} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(config, dict):
        raise ValueError(
            f"Configuration file {CONFIG_FILE} must contain a JSON object"
        )

    # Top-level directory list key: directories or dirs
    directories = config.get("directories", config.get("dirs"))
    if not isinstance(directories, list):
        raise ValueError(
            f'Configuration file {CONFIG_FILE} must contain a "directories" '
            f'array, for example: {{ "directories": ["D:/scripts"] }}'
        )

    config["directories"] = [normalize_dir_item(d) for d in directories]
    return config


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def is_supported(entry: Path) -> bool:
    """
    Return True if the file is of a supported type.

    Supported means either the extension is in SUPPORTED_EXTS, or (on
    non-Windows systems) the file is executable.
    """
    if not entry.is_file():
        return False
    if entry.suffix.lower() in SUPPORTED_EXTS:
        return True
    # Executable file without an extension (e.g. a shebang script on Linux)
    if os.name != "nt" and not entry.suffix and os.access(str(entry), os.X_OK):
        return True
    return False


def match_filters(entry: Path, filters: list) -> bool:
    """
    Return True if the filename matches any of the glob patterns.

    Matching is case-insensitive. An empty pattern list matches everything.
    Patterns support * ? and [], for example:
        *.jar       every jar file
        myapp*.jar  jars whose name starts with "myapp"
        test_??.py  test_01.py and friends
    """
    if not filters:
        return True
    name = entry.name.lower()
    return any(fnmatch.fnmatchcase(name, f.lower()) for f in filters)


def script_key(entry: Path) -> str:
    """
    Return the lookup name for a file: its filename without the extension.
    For example abc.py -> abc.
    """
    return entry.stem


# ---------------------------------------------------------------------------
# Scanning and indexing
# ---------------------------------------------------------------------------

def iter_dir_entries(root: Path, recurse: bool):
    """Yield files in a directory, recursing into subdirectories if asked."""
    if recurse:
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip directories that never hold runnable scripts
            dirnames[:] = sorted(
                d for d in dirnames if d not in ("__pycache__", ".git", ".svn")
            )
            for filename in sorted(filenames):
                yield Path(dirpath) / filename
    else:
        for entry in sorted(root.iterdir()):
            if entry.is_file():
                yield entry


def scan_scripts(directories):
    """
    Scan the configured directories and collect supported files.

    Args:
        directories (list[dict]): entries produced by normalize_dir_item,
            in priority order.

    Returns:
        tuple:
            index      (dict[str, Path]): lookup name -> full path
            type_index (dict[str, Path]): "name.ext" -> path, for explicitly
                                          choosing a type
            rejected   (list[tuple[Path, str]]): files excluded by filters,
                                                 with their directory
            dupes      (list[tuple[Path, Path]]): shadowed files (new, kept)
    """
    index = {}
    type_index = {}
    rejected = []
    dupes = []

    for item in directories:
        root = Path(item["path"]).expanduser()
        if not root.is_dir():
            # Skip missing directories with a warning; keep scanning the rest
            print(f"[warning] Directory not found, skipped: {root}", file=sys.stderr)
            continue

        filters = item.get("filters") or []
        exts = item.get("exts")

        for entry in iter_dir_entries(root, item.get("recurse", False)):
            if not is_supported(entry):
                continue
            if exts is not None and entry.suffix.lower() not in exts:
                continue
            if not match_filters(entry, filters):
                rejected.append((entry, item["path"]))
                continue

            name = script_key(entry)
            typed = f"{name}{entry.suffix.lower()}".lower()
            if typed in type_index:
                dupes.append((entry, type_index[typed]))
                continue

            type_index[typed] = entry
            if name in index:
                dupes.append((entry, index[name]))
                continue
            index[name] = entry

    return index, type_index, rejected, dupes


def explain_dir_rules(directories) -> list:
    """Describe the scan rules of each directory, for ls --all output."""
    lines = []
    for item in directories:
        parts = [item["path"]]
        if item.get("filters"):
            parts.append("filters=" + ",".join(item["filters"]))
        if item.get("exts"):
            parts.append("exts=" + ",".join(sorted(item["exts"])))
        if item.get("recurse"):
            parts.append("recurse")
        lines.append("  - " + "  ".join(parts))
    return lines


# ---------------------------------------------------------------------------
# Subcommands: ls / help
# ---------------------------------------------------------------------------

def cmd_list(index, verbose: bool, config: dict, type_index=None,
             rejected=None, show_all: bool = False) -> None:
    """
    Print the list of discovered scripts.

    Args:
        index      (dict[str, Path]): the script index.
        verbose    (bool):            also print type and full path.
        config     (dict):            configuration, used to show scan rules.
        type_index (dict[str, Path]): index keyed by "name.ext".
        rejected   (list):            files excluded by filters.
        show_all   (bool):            show scan rules and excluded files.
    """
    if show_all:
        print("Scan rules:", file=sys.stderr)
        for line in explain_dir_rules(config.get("directories", [])):
            print(line, file=sys.stderr)
        print("", file=sys.stderr)

    if not index:
        print("No scripts found. Check the directories and filters in mymy.json.")
        return

    if verbose:
        width = max(len(n) for n in index) + 2
        for name in sorted(index):
            path = index[name]
            print(f"{name:<{width}} [{path.suffix.lower() or '-'}]  {path}")
    else:
        for name in sorted(index):
            print(name)

    if show_all and rejected:
        print("\nFiles excluded by filters:", file=sys.stderr)
        for path, directory in rejected:
            print(f"  {path}  (from {directory})", file=sys.stderr)


def cmd_help() -> None:
    """Print usage help."""
    print(__doc__.strip())


# ---------------------------------------------------------------------------
# Locating interpreters
# ---------------------------------------------------------------------------

def find_powershell() -> str | None:
    """Return pwsh (PowerShell 7+) if available, otherwise Windows PowerShell."""
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def find_bash() -> str | None:
    """Return bash, falling back to common Git Bash locations on Windows."""
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        ]
        for path in candidates:
            if path.is_file():
                return str(path)
    return None


def find_java() -> str | None:
    """Return the java executable, from PATH or JAVA_HOME."""
    found = shutil.which("java")
    if found:
        return found
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / ("java.exe" if os.name == "nt" else "java")
        if candidate.is_file():
            return str(candidate)
    return None


def read_class_package(class_file: Path) -> str | None:
    """
    Read the constant pool of a .class file and return its package name.

    Returns:
        str | None: the package (e.g. "com.demo"); an empty string for the
                    default package; None if the file cannot be parsed.

    Layout used here: magic(4) + minor/major version(4) + constant_pool_count(2)
    followed by the constant pool. A CONSTANT_Class(7) entry only holds a
    name_index, so Utf8 entries and Class references are collected first and
    resolved through this_class at the end.
    """
    try:
        data = class_file.read_bytes()
    except OSError:
        return None

    try:
        if data[:4] != b"\xca\xfe\xba\xbe":
            return None

        pos = 8  # Skip magic number and version fields
        cp_count = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2

        utf8 = {}
        class_refs = {}  # index -> name_index

        idx = 1
        while idx < cp_count:
            tag = data[pos]
            pos += 1
            if tag == 1:  # Utf8
                length = int.from_bytes(data[pos:pos + 2], "big")
                pos += 2
                utf8[idx] = data[pos:pos + length].decode("utf-8", "replace")
                pos += length
            elif tag in (7, 8, 16, 19, 20):  # Class, String, MethodType, Module, Package
                if tag == 7:
                    class_refs[idx] = int.from_bytes(data[pos:pos + 2], "big")
                pos += 2
            elif tag in (15,):  # MethodHandle
                pos += 3
            elif tag in (3, 4, 9, 10, 11, 12, 17, 18):  # Fixed four-byte entries
                pos += 4
            elif tag in (5, 6):  # Long and Double occupy two slots
                pos += 8
                idx += 1
            else:
                return None  # Unknown tag: give up on parsing
            idx += 1

        # After the constant pool: access_flags(2) + this_class(2)
        this_class = int.from_bytes(data[pos + 2:pos + 4], "big")
        name_index = class_refs.get(this_class)
        if name_index is None:
            return None
        internal_name = utf8.get(name_index, "")
        if "/" not in internal_name:
            return ""
        return internal_name.rsplit("/", 1)[0].replace("/", ".")
    except (IndexError, ValueError):
        return None


def java_class_info(script_path: Path) -> tuple[str, Path]:
    """
    Work out the (fully qualified class name, classpath root) of a .class file.

    The real package name is read from the class file constant pool and the
    classpath root is then found by walking up one level per package segment:

        <root>/com/demo/Hello.class  ->  ("com.demo.Hello", <root>)
        <root>/Hello.class           ->  ("Hello", <root>)

    If the file cannot be parsed, the package structure is inferred from the
    directory names instead.
    """
    stem = script_path.stem
    package = read_class_package(script_path)

    if package is not None:
        depth = len(package.split(".")) if package else 0
        root = script_path.parent
        for _ in range(depth):
            root = root.parent
        class_name = f"{package}.{stem}" if package else stem
        return class_name, root

    # Fallback: infer packages from directory names (lowercase identifiers)
    parts = [stem]
    current = script_path.parent
    while current.name.isidentifier() and current.name.islower():
        parts.insert(0, current.name)
        current = current.parent
    return ".".join(parts), current


# ---------------------------------------------------------------------------
# Running scripts
# ---------------------------------------------------------------------------

def build_command(script_path: Path, script_args: list) -> list | str | None:
    """
    Build the command that actually runs the given file.

    Returns:
        list[str] | str: the command (a list is passed straight to subprocess;
                         a string means shell=True is required)
        None:            no command could be built (missing interpreter);
                         an error has already been printed.

    Args:
        script_path (Path): full path of the script to run.
        script_args (list): arguments to pass (excluding the script name).
    """
    suffix = script_path.suffix.lower()
    path_str = str(script_path)

    # --- Python script: run with the current interpreter ---
    if suffix == ".py":
        # Add the script's directory to sys.path so it can import siblings
        script_dir = str(script_path.parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        return [sys.executable, path_str, *script_args]

    # --- PowerShell script ---
    if suffix in PS1_EXTS:
        ps = find_powershell()
        if ps is None:
            print(
                "[error] PowerShell (pwsh / powershell) was not found; "
                "cannot run .ps1",
                file=sys.stderr,
            )
            return None
        return [
            ps,
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", path_str,
            *script_args,
        ]

    # --- Batch files .bat / .cmd: run through cmd.exe ---
    if suffix in BATCH_EXTS:
        if os.name == "nt":
            comspec = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd.exe"
            # /d skips AutoRun, /c runs the command and exits.
            # cmd treats everything after /c as one command line, so the script
            # path must not be quoted; only arguments containing spaces are.
            def _quote_arg(a: str) -> str:
                a = str(a)
                if a and (" " in a or "\t" in a or '"' in a):
                    return '"' + a.replace('"', '""') + '"'
                return a

            line = " ".join([path_str, *(_quote_arg(a) for a in script_args)])
            return [comspec, "/d", "/c", line]
        # Non-Windows: fall back to a shell command line
        return " ".join([shlex.quote(path_str), *[shlex.quote(a) for a in script_args]])

    # --- Shell script .sh: run with bash ---
    if suffix in SH_EXTS:
        bash = find_bash()
        if bash is None:
            print(
                "[error] bash was not found; cannot run .sh "
                "(install Git for Windows on Windows)",
                file=sys.stderr,
            )
            return None
        return [bash, path_str, *script_args]

    # --- Java jar: java -jar ---
    if suffix == ".jar":
        java = find_java()
        if java is None:
            print(
                "[error] java was not found (install a JDK/JRE or set JAVA_HOME)",
                file=sys.stderr,
            )
            return None
        return [java, "-jar", path_str, *script_args]

    # --- Java class: java -cp <root> <class name> ---
    if suffix == ".class":
        java = find_java()
        if java is None:
            print(
                "[error] java was not found (install a JDK/JRE or set JAVA_HOME)",
                file=sys.stderr,
            )
            return None
        class_name, class_root = java_class_info(script_path)
        return [java, "-cp", str(class_root), class_name, *script_args]

    # --- Node.js script ---
    if suffix in JS_EXTS:
        node = shutil.which("node")
        if node is None:
            print(
                "[error] node was not found; cannot run JavaScript files",
                file=sys.stderr,
            )
            return None
        return [node, path_str, *script_args]

    # --- VBScript / Windows Script File: run with cscript ---
    if suffix in VBS_EXTS:
        cscript = shutil.which("cscript") if os.name == "nt" else None
        if cscript is None:
            print(
                "[error] cscript was not found; cannot run .vbs / .wsf",
                file=sys.stderr,
            )
            return None
        return [cscript, "//nologo", path_str, *script_args]

    # --- Executables and other scripts: run directly ---
    return [path_str, *script_args]


def run_script(script_path: Path, script_args: list) -> int:
    """
    Run the given script and wait for it to finish.

    Args:
        script_path (Path): full path of the script to run.
        script_args (list): arguments to pass (excluding the script name).

    Returns:
        int: the exit code of the child process.
    """
    cmd = build_command(script_path, script_args)
    if cmd is None:
        return 1

    if isinstance(cmd, str):
        print(f"[run] {cmd}", file=sys.stderr)
        try:
            return subprocess.run(cmd, shell=True).returncode
        except OSError as exc:
            print(f"[error] Execution failed: {exc}", file=sys.stderr)
            return 1

    # Keep child output encoding consistent with the terminal
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    print(f"[run] {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, env=env)
        return result.returncode
    except OSError as exc:
        print(f"[error] Execution failed: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    """
    Parse the command line and dispatch to the matching logic.

    Args:
        argv (list): arguments without the interpreter and script name,
                     i.e. sys.argv[1:].

    Returns:
        int: process exit code.
    """
    # No arguments: print help
    if not argv:
        cmd_help()
        return 0

    command = argv[0]

    # help does not need the configuration
    if command in (CMD_HELP, "-h", "--help"):
        cmd_help()
        return 0

    # Load the configuration and scan the directories
    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    index, type_index, rejected, dupes = scan_scripts(config["directories"])

    # ---- Subcommand: ls ----
    if command == CMD_LIST:
        verbose = "-v" in argv[1:] or "--verbose" in argv[1:]
        show_all = "--all" in argv[1:] or "-a" in argv[1:]
        cmd_list(index, verbose, config, type_index, rejected, show_all)
        return 0

    # ---- Normal run: mymy.py abc arg1 arg2 ----
    # Try an explicit type first (abc.py / app.jar), then fall back to the name
    script_path = type_index.get(command.lower()) or index.get(command)
    if script_path is None:
        print(
            NOT_FOUND_HINT.format(name=command, cfg=CONFIG_FILE),
            file=sys.stderr,
        )
        return 1

    return run_script(script_path, argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
