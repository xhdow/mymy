# mymy

A single-file script launcher. It collects scripts scattered across several
directories into one flat index of command names, so you can run any of them
by name without typing a full path.

```console
$ python mymy.py ls
add_vol
crlf
http_echo
sebook
```

## Features

- **Run scripts by name, from anywhere.** Add directories once, then call
  `mymy.py <name> [args...]`.
- **Understands many script types.** Each file is launched with the right
  interpreter instead of relying on shell file associations.
- **Per-directory filters.** Use glob patterns to expose only the files you
  want from a directory.
- **Arguments and exit codes pass through unchanged.** The child process runs
  in your current working directory, and its exit code becomes the exit code
  of `mymy.py`.
- **Clear errors.** If an interpreter such as `java`, `node`, `bash` or
  PowerShell is missing, you get a readable message instead of a traceback.

## Supported file types

| Extension | Launched with |
| --- | --- |
| `.py` | Current Python interpreter; the script's directory is added to `sys.path` |
| `.ps1` | PowerShell (`pwsh` preferred, falls back to `powershell`) |
| `.bat`, `.cmd` | `cmd.exe` |
| `.sh` | `bash` (on Windows, Git Bash is located automatically) |
| `.jar` | `java -jar` |
| `.class` | `java -cp <classpath root> <fully.qualified.Name>` |
| `.exe`, `.com` | Run directly |
| `.js`, `.mjs`, `.cjs` | `node` |
| `.vbs`, `.wsf` | `cscript` |
| Any executable file | Run directly (non-Windows) |

For `.class` files, `mymy` reads the constant pool of the class file to
recover its real package declaration and derives the classpath root from it,
so both default-package and packaged classes work:

```console
$ python mymy.py PkgHello --verbose
[run] java -cp C:\build\classes com.demo.PkgHello --verbose
```

## Requirements

- Python 3.10 or newer.
- The interpreter for the file types you intend to run (for example, Java for
  `.jar` / `.class`, Node.js for `.js`, Git Bash for `.sh` on Windows).

Nothing needs to be installed: `mymy.py` uses only the standard library.

## Configuration

Configuration lives in `mymy.json`, next to `mymy.py`.

```json
{
  "directories": [
    "F:/Project/Python/my/dist",
    { "path": "E:/workspace/utils" },
    {
      "path": "F:/Project/Java/sebook/dist",
      "filters": ["*.jar", "*.cmd", "myapp*.jar"]
    }
  ]
}
```

Each entry in `directories` is either a plain string or an object with the
following fields:

| Field | Required | Description |
| --- | --- | --- |
| `path` | Yes | Directory to scan. `dir` and `directory` are accepted as aliases. |
| `filters` | No | List of filename glob patterns, e.g. `["*.jar", "myapp*.jar"]`. Case-insensitive; supports `*`, `?` and `[]`. If omitted, every supported file in the directory matches. |
| `recurse` | No | Scan subdirectories as well. Defaults to `false` (only the top level is scanned). |
| `exts` | No | Restrict scanning to these extensions, narrowing the result further after `filters`. |

The top-level key may also be spelled `dirs`.

### Filter behaviour

Item order inside `directories` is priority order. The first directory that
provides a given name wins. Files that exist but are excluded by `filters` are
not indexed; use `ls --all` to see them.

## Usage

```console
python mymy.py ls            # list every available script name
python mymy.py ls -v         # also show the type and full path
python mymy.py ls --all      # also show scan rules and files excluded by filters
python mymy.py abc arg1 arg2 # run "abc", passing arg1 and arg2 through
python mymy.py help          # show built-in help
```

### Choosing between same-named files

If two files share a base name but differ in type, for example `abc.py` and
`abc.cmd`, pass the extension to pick one explicitly:

```console
python mymy.py abc.py        # runs abc.py
python mymy.py abc.cmd       # runs abc.cmd
python mymy.py abc           # runs whichever was found first
```

### Example

Given this configuration:

```json
{
  "directories": [
    "D:/tools/scripts",
    {
      "path": "D:/build/libs",
      "filters": ["myapp*.jar"]
    }
  ]
}
```

`ls` might report:

```console
$ python mymy.py ls -v
deploy    [.py]   D:\tools\scripts\deploy.py
myapp     [.jar]  D:\build\libs\myapp-2.1.jar
```

and the whole workflow becomes:

```console
$ python mymy.py deploy --env prod
[run] C:\Python\python.exe D:\tools\scripts\deploy.py --env prod
```

## How names are resolved

- A file's lookup name is its filename without the extension (`crlf.py` →
  `crlf`). Adding the extension (`crlf.py`) is also accepted.
- Directories are scanned in the order listed; the first match wins, and later
  duplicates are ignored.
- Arguments are passed to the target program verbatim without being
  re-parsed by a shell.

## Limitations

- `.bat` / `.cmd` files go through `cmd.exe`, which performs its own quoting.
  Arguments containing spaces are therefore passed along with quotes intact.
- A missing configured directory is reported as a warning and skipped rather
  than aborting the scan.
