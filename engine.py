import base64
import getpass
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("PROJECTS_ROOT") or (str(Path.home() / "public_html") if (Path.home() / "public_html").is_dir() else str(BASE.parent))).resolve()
DATA = BASE / "data"
JOBDIR = DATA / "jobs"
WORK = DATA / "work"
PATCHED = DATA / "patched.json"
JOBDIR.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)

AI_URL = "https://api.anthropic.com/v1/messages"
AI_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
AI_MAX_CALLS = int(os.environ.get("AI_MAX_CALLS", "12"))
AI_TOKEN_BUDGET = int(os.environ.get("AI_TOKEN_BUDGET", "200000"))
AI_MAX_FILE = 60000
AI_ALLOWED = ("app/", "routes/", "resources/views/")
AI_SYSTEM = (
    "You fix PHP errors in an older Laravel application that was migrated to Laravel 12 on PHP 8.3. "
    "You receive one error and the file it points to. Return the smallest set of exact search and replace edits "
    "that fixes this error. Each old string must occur exactly once in the file and must be copied exactly. "
    "Do not add comments, do not reformat, do not rename things that work, do not touch unrelated code. "
    "Use Laravel 12 equivalents for removed helpers and facades. "
    "Never delete application code, never comment code out, and never touch anything related to the citigate payment gateway. "
    "If a class or package was removed and cannot be replaced safely, return no edits and explain in the summary. "
    "If this file is not the cause, or the error cannot be fixed by editing this file, return no edits and explain in the summary. "
    "Always answer by calling the apply_fix tool and never reply with plain text."
)
AI_TOOL = {
    "name": "apply_fix",
    "description": "Return the edits that fix the error in the given file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                    "required": ["old", "new"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["edits", "summary"],
    },
}


def find_php():
    cands = [os.environ.get("PHP_BIN")] + ["/opt/cpanel/ea-php%s/root/usr/bin/php" % v for v in ("83", "84", "82", "85")] + [shutil.which("php")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return "php"


PHP = find_php()


def find_composer():
    if os.environ.get("COMPOSER_BIN"):
        return os.environ["COMPOSER_BIN"].split()
    w = shutil.which("composer")
    if w:
        return [w]
    for c in (Path.home() / "composer.phar", BASE / "composer.phar"):
        if c.exists():
            return [PHP, str(c)]
    return ["composer"]


COMPOSER = find_composer()


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k:
        f = DATA / "anthropic.key"
        if f.is_file():
            k = f.read_text().strip()
    return k


def envx():
    e = dict(os.environ, COMPOSER_ALLOW_SUPERUSER="1", COMPOSER_MEMORY_LIMIT="-1", COMPOSER_NO_INTERACTION="1")
    e["PATH"] = str(Path(PHP).parent) + os.pathsep + e.get("PATH", "/usr/bin:/bin")
    e.setdefault("HOME", str(Path.home()))
    return e


def resolve(project):
    p = Path(project)
    return (p if p.is_absolute() else ROOT / project).resolve()


SKIP = {"php", "laravel/framework", "laravel/tinker", "laravel/ui", "laravel/helpers", "fideloper/proxy",
        "fruitcake/laravel-cors", "facade/ignition", "fzaninotto/faker", "swiftmailer/swiftmailer"}
DROP = re.compile(r"instamojo|paytm|paypal|nexmo|paystack|corecomponent|cinetpay|(^|[/\-_])ups($|[/\-_])", re.I)
ENV_ERR = re.compile(r"SQLSTATE\[|Access denied for user|Connection refused|Unknown database", re.I)
STD = {"EncryptCookies", "VerifyCsrfToken", "TrustProxies", "TrustHosts", "CheckForMaintenanceMode",
       "PreventRequestsDuringMaintenance", "TrimStrings", "ConvertEmptyStringsToNull", "RedirectIfAuthenticated"}
MW_BUILTIN = {"auth", "auth.basic", "auth.session", "cache.headers", "can", "guest", "password.confirm",
              "precognitive", "signed", "throttle", "verified"}
RENAME = ["vendor", "bootstrap", "config", "artisan", "composer.json", "composer.lock", "app/Http/Kernel.php",
          "app/Console/Kernel.php", "app/Exceptions/Handler.php", "app/Providers/RouteServiceProvider.php",
          "index.php", ".htaccess"]
CFG_OBSOLETE = {"app.php": {"providers", "aliases", "log", "log_level", "log_max_files", "asset_url", "debug_blacklist"},
                "filesystems.php": {"cloud"},
                "mail.php": {"sendmail", "markdown", "pretend"}}
CITIGATE_PATHS = ("citigate/success", "citigate/fail", "citigate/cancel", "citigate/ipn")
LEAD = re.compile(r"(?:\s|//[^\n]*|#[^\n]*|/\*.*?\*/)*", re.S)
LOCAL_DIR = "packages_local"
ARTISAN = r"""#!/usr/bin/env php
<?php

use Illuminate\Foundation\Application;
use Symfony\Component\Console\Input\ArgvInput;

define('LARAVEL_START', microtime(true));

require __DIR__.'/vendor/autoload.php';

/** @var Application $app */
$app = require_once __DIR__.'/bootstrap/app.php';

$status = $app->handleCommand(new ArgvInput);

exit($status);
"""
HANDLER_PHP = r"""<?php

namespace App\Exceptions;

use Illuminate\Foundation\Exceptions\Handler as ExceptionHandler;

class Handler extends ExceptionHandler
{
    protected $dontFlash = [
        'current_password',
        'password',
        'password_confirmation',
    ];

    public function register(): void
    {
        //
    }
}
"""
CONTROLLER_PHP = r"""<?php

namespace App\Http\Controllers;

use Illuminate\Foundation\Auth\Access\AuthorizesRequests;
use Illuminate\Foundation\Validation\ValidatesRequests;
use Illuminate\Routing\Controller as BaseController;

abstract class Controller extends BaseController
{
    use AuthorizesRequests, ValidatesRequests;
}
"""
ROUTE_ACTION = re.compile(r"""(['"])(\\{0,2}(?:[A-Za-z_]\w*\\{1,2})*[A-Za-z_]\w*Controller)@(\w+)\1""")
ROUTE_RESOURCE = re.compile(r"""(Route::(?:resource|apiResource)\(\s*['"][^'"]+['"]\s*,\s*)(['"])(\\{0,2}(?:[A-Za-z_]\w*\\{1,2})*[A-Za-z_]\w*Controller)\2""")


def is_laravel(p):
    if not (p / "composer.json").is_file():
        return False
    if (p / "artisan").is_file():
        return True
    try:
        req = json.loads((p / "composer.json").read_text()).get("require", {})
    except Exception:
        return False
    return "laravel/framework" in req


def web_parent(p):
    idx = p.parent / "index.php"
    if not idx.is_file():
        return False
    try:
        t = idx.read_text(errors="replace")
    except OSError:
        return False
    return (p.name + "/vendor/autoload.php") in t or (p.name + "/bootstrap/app.php") in t


def access_targets(p):
    out = [p]
    for rel in (".env", "public/index.php"):
        if (p / rel).is_file():
            out.append(p / rel)
    return out


def check_access(job):
    me = os.geteuid()
    dry = bool(job.opts.get("dry_run"))
    fix, blocked = [], []
    for _ in range(2):
        fix, blocked = [], []
        for t in access_targets(job.path):
            need = os.W_OK | os.X_OK if t.is_dir() else os.W_OK
            if os.access(str(t), need):
                continue
            (fix if t.stat().st_uid == me else blocked).append(t)
        if blocked or dry or not fix:
            break
        for t in fix:
            t.chmod(t.stat().st_mode | (0o700 if t.is_dir() else 0o600))
            job.log("permissions fixed on " + str(t))
    left = blocked + ([] if dry else fix)
    if left:
        first = left[0]
        try:
            owner = first.owner()
        except Exception:
            owner = str(first.stat().st_uid)
        user = getpass.getuser()
        raise RuntimeError("not writable by %s, owned by %s: %s%s. Run as root: chown -R %s: %s" % (
            user, owner, first, " and %d more" % (len(left) - 1) if len(left) > 1 else "", user, job.path))
    if fix:
        job.note("permissions on %d paths would be fixed by the real run" % len(fix))


class Job:
    def __init__(self, project, opts, data=None):
        if data:
            self.__dict__.update(data)
            self.__dict__.setdefault("notes", [])
            self.__dict__.setdefault("conclusion", None)
        else:
            self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
            self.project = project
            rp = resolve(project)
            try:
                rel = rp.relative_to(ROOT).as_posix()
            except ValueError:
                rel = ""
            self.name = rel if rel and rel != "." else rp.name
            self.opts = opts
            self.pid = 0
            self.status = "queued"
            self.steps = [{"key": k, "title": t, "status": "pending", "error": None, "secs": 0} for k, t, _ in plan(opts)]
            self.manifest = []
            self.warnings = []
            self.notes = []
            self.state = {}
            self.conclusion = None
            self.created = time.time()
            self.rolled_back = False
        self.path = resolve(self.project)

    def log(self, m):
        with open(JOBDIR / (self.id + ".log"), "a") as f:
            f.write(time.strftime("%H:%M:%S ") + str(m) + "\n")

    def save(self):
        d = {k: v for k, v in self.__dict__.items() if k != "path"}
        (JOBDIR / (self.id + ".json")).write_text(json.dumps(d))

    def view(self):
        d = {k: v for k, v in self.__dict__.items() if k not in ("path", "state", "manifest")}
        d["actions"] = len(self.manifest)
        d["log_tail"] = ""
        return d

    def brief(self):
        return {"id": self.id, "project": self.name, "status": self.status, "created": self.created,
                "rolled_back": self.rolled_back, "patch": bool((self.opts or {}).get("patch"))}

    def warn(self, m):
        self.warnings.append(m)
        self.log("WARNING " + m)

    def note(self, m):
        self.notes.append(m)
        self.log("NOTE " + m)


def sh(job, cmd, check=True, timeout=1800, cwd=None):
    job.log("$ " + " ".join(cmd))
    env = envx()
    pr = subprocess.Popen(cmd, cwd=cwd or job.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          errors="replace", env=env)
    timer = threading.Timer(timeout, pr.kill)
    timer.start()
    out = []
    for line in pr.stdout:
        out.append(line)
        job.log(line.rstrip())
    pr.wait()
    timer.cancel()
    text = "".join(out)
    if check and pr.returncode != 0:
        raise RuntimeError("%s exited %s: %s" % (cmd[0], pr.returncode, text[-900:]))
    return pr.returncode, text


def backup(job, p):
    b = p.with_name(p.name + ".bak-" + job.id)
    if not b.exists():
        shutil.copy2(p, b)
        job.manifest.append({"t": "modify", "path": str(p.relative_to(job.path)), "backup": str(b.relative_to(job.path))})
    return b


def write(job, rel, text):
    p = job.path / rel
    if p.exists():
        if p.read_text(errors="replace") == text:
            return False
        backup(job, p)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        job.manifest.append({"t": "create", "path": rel})
    p.write_text(text)
    job.log("wrote " + rel)
    return True


def move(job, rel):
    src = job.path / rel
    if not src.exists():
        return False
    dst = job.path / (rel + "_old_" + job.id)
    src.rename(dst)
    job.manifest.append({"t": "rename", "from": rel, "to": rel + "_old_" + job.id})
    job.log("renamed %s -> %s" % (rel, dst.name))
    return True


def adopt(job, src, rel):
    shutil.move(str(src), str(job.path / rel))
    job.manifest.append({"t": "create", "path": rel})
    job.log("installed " + rel)


def read(job, rel):
    p = job.path / rel
    return p.read_text(errors="replace") if p.is_file() else ""


def read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}


def find_old_kernel(job):
    for f in sorted((job.path / "app/Http").glob("Kernel*")):
        t = f.read_text(errors="replace")
        if "routeMiddleware" in t or "middlewareAliases" in t:
            return t
    return ""


def http_status(u):
    try:
        return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "upgrade-tool"}), timeout=25).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return str(e)


def laravel_version(path):
    app = path / "vendor/laravel/framework/src/Illuminate/Foundation/Application.php"
    if app.is_file():
        m = re.search(r"const VERSION = '([^']+)'", app.read_text(errors="replace"))
        if m:
            return m.group(1)
    lock = path / "composer.lock"
    if lock.is_file():
        try:
            for pkg in json.loads(lock.read_text()).get("packages", []):
                if pkg.get("name") == "laravel/framework":
                    return pkg["version"].lstrip("v")
        except Exception:
            pass
    try:
        req = json.loads((path / "composer.json").read_text()).get("require", {}).get("laravel/framework", "")
    except Exception:
        return ""
    m = re.search(r"\d+(\.\d+)*", req)
    return m.group(0) if m else ""


def installed_major(path):
    app = path / "vendor/laravel/framework/src/Illuminate/Foundation/Application.php"
    if not app.is_file():
        return 0
    m = re.search(r"const VERSION = '(\d+)", app.read_text(errors="replace"))
    return int(m.group(1)) if m else 0


def has_citigate(path):
    if (path / "app/Http/Controllers/CitigateController.php").is_file():
        return True
    r = path / "routes"
    if r.is_dir():
        for f in r.glob("*.php"):
            if "citigate" in f.read_text(errors="replace").lower():
                return True
    return False


def gateways_in_use(job):
    items = [x.strip().lower() for x in str(job.opts.get("gateways") or "").split(",") if x.strip()]
    if not items and job.state.get("citigate"):
        items = ["citigate"]
    return items


def probe_paths(job):
    return job.opts.get("probe_paths") or ["/"]


def probe(job, paths):
    if not paths:
        return {}
    b64 = base64.b64encode(json.dumps(paths).encode()).decode()
    host = urlparse(job.opts.get("app_url") or "").netloc or "localhost"
    hb64 = base64.b64encode(host.encode()).decode()
    code = ("$_SERVER['HTTP_HOST']=$_SERVER['HTTP_HOST']??base64_decode('%s');"
            "$k=app(Illuminate\\Contracts\\Http\\Kernel::class);$o=[];"
            "foreach(json_decode(base64_decode('%s'),true) as $u){"
            "try{$o[$u]=$k->handle(Illuminate\\Http\\Request::create($u))->getStatusCode();}"
            "catch(\\Throwable $e){$o[$u]=500;}}"
            "echo 'PROBE'.json_encode($o);") % (hb64, b64)
    rc, out = sh(job, [PHP, "artisan", "tinker", "--execute=" + code], check=False, timeout=300)
    m = re.search(r"PROBE(\{.*\})", out)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def code_files(path):
    out, size = {}, 0
    for base in ("app", "routes", "resources/views", "database"):
        d = path / base
        if not d.is_dir():
            continue
        for f in d.rglob("*.php"):
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            out[str(f.relative_to(path))] = t
            size += len(t)
            if size > 60000000:
                return out
    return out


def ns_used(blob, ns):
    return (ns + "\\") in blob or (ns.replace("\\", "\\\\") + "\\\\") in blob


def alias_used(blob, alias):
    return re.search(r"(?<![\w\\$])" + re.escape(alias) + r"::", blob) is not None


def pkg_meta(path, name, lock=None):
    f = path / "vendor" / name / "composer.json"
    if f.is_file():
        cj = read_json(f)
        if cj:
            return cj
    if lock:
        for p in (lock.get("packages") or []) + (lock.get("packages-dev") or []):
            if p.get("name") == name:
                return p
    return {}


def pkg_namespaces(path, name, lock=None):
    cj = pkg_meta(path, name, lock)
    out = []
    au = cj.get("autoload") or {}
    for key in ("psr-4", "psr-0"):
        for ns in (au.get(key) or {}):
            ns = ns.rstrip("\\")
            if ns:
                out.append(ns)
    return out


def pkg_aliases(path, name, lock=None):
    cj = pkg_meta(path, name, lock)
    al = ((cj.get("extra") or {}).get("laravel") or {}).get("aliases") or {}
    return list(al) if isinstance(al, dict) else []


def culprits(lock, roots, target):
    graph = {}
    for p in (lock.get("packages") or []) + (lock.get("packages-dev") or []):
        graph[p.get("name")] = list((p.get("require") or {}).keys())
    out = []
    for r in roots:
        seen, stack = set(), [r]
        while stack:
            n = stack.pop()
            if n == target:
                out.append(r)
                break
            if n in seen:
                continue
            seen.add(n)
            stack.extend(graph.get(n, []))
    return out


def drop_pkg(job, name, why, lock=None):
    if name not in job.state["dropped"]:
        job.state["dropped"].append(name)
    job.state["dropped_ns"][name] = pkg_namespaces(job.path, name, lock)
    job.state.setdefault("dropped_alias", {})[name] = pkg_aliases(job.path, name, lock)
    job.log("dropped %s (%s)" % (name, why))


def localize(job, name, out):
    if name in job.state.get("flagged", []):
        return False
    if name in job.state.get("local_pkgs", {}):
        return False
    if not re.search(re.escape(name) + r"(?:\[[^\]]*\])? [^\n]*?requires? php", out):
        return False
    src = job.path / "vendor" / name
    cj = read_json(src / "composer.json")
    if not cj or (cj.get("extra") or {}).get("laravel"):
        return False
    dest = job.path / LOCAL_DIR / name
    if not (job.path / LOCAL_DIR).exists():
        job.manifest.append({"t": "create", "path": LOCAL_DIR})
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    ver = "1.0.0"
    lock = read_json(job.path / "composer.lock")
    for p in (lock.get("packages") or []) + (lock.get("packages-dev") or []):
        if p.get("name") == name and p.get("version"):
            ver = str(p["version"]).lstrip("v")
            break
    cj["name"] = name
    cj["version"] = ver
    cj["require"] = {k: v for k, v in (cj.get("require") or {}).items() if k not in ("php", "hhvm")}
    cj.pop("require-dev", None)
    cj.pop("scripts", None)
    (dest / "composer.json").write_text(json.dumps(cj, indent=4))
    job.state["local_pkgs"][name] = str(dest)
    job.log("kept %s from the existing install with its PHP 7 limit removed (%s)" % (name, ver))
    return True


def csrf_exceptions(job):
    t = read(job, "app/Http/Middleware/VerifyCsrfToken.php")
    m = re.search(r"\$except\s*=\s*\[(.*?)\];", t, re.S)
    items = [x.strip("/") for x in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))] if m else []
    if job.state.get("citigate") or has_citigate(job.path):
        items.extend(CITIGATE_PATHS)
    return [i for i in dict.fromkeys(items) if i]


def s_preflight(job):
    p = job.path
    repair = bool(job.opts.get("repair"))
    if not is_laravel(p):
        raise RuntimeError("composer.json missing or does not require laravel/framework: not a Laravel project")
    old = json.loads((p / "composer.json").read_text())
    ver = laravel_version(p)
    if installed_major(p) >= 12 and not repair:
        raise RuntimeError("already on Laravel %s, use repair mode" % ver)
    _, v = sh(job, [PHP, "-r", "echo PHP_VERSION;"])
    job.log("PHP " + v.strip())
    if tuple(int(x) for x in v.strip().split(".")[:2]) < (8, 2):
        raise RuntimeError("PHP %s is below 8.2" % v.strip())
    check_access(job)
    job.state["nested"] = web_parent(p)
    if job.state["nested"]:
        job.log("web root is %s, its index.php and .htaccess are kept, the project is %s" % (p.parent, p))
    if (p / "artisan").is_file():
        sh(job, [PHP, "artisan", "--version"], check=False)
    elif repair and not job.opts.get("dry_run"):
        write(job, "artisan", ARTISAN)
        os.chmod(p / "artisan", 0o755)
        job.note("artisan was missing and was created")
    if not repair:
        sh(job, COMPOSER + ["--version"])
    free = shutil.disk_usage(p).free
    if free < 1 << 29:
        raise RuntimeError("less than 512 MB free disk")
    job.state["old_composer"] = old
    job.state["old_kernel"] = read(job, "app/Http/Kernel.php") or find_old_kernel(job)
    job.state["user_model"] = "\\App\\Models\\User" if (p / "app/Models/User.php").exists() else "\\App\\User"
    job.state["from_version"] = ver
    job.state["citigate"] = has_citigate(p)
    job.log("laravel version: %s, mode: %s, kernel found: %s, citigate: %s" % (
        ver or "unknown", "repair" if repair else "full", bool(job.state["old_kernel"]), job.state["citigate"]))


def s_skelprep(job):
    sk = DATA / "skeleton"
    if not (sk / "composer.json").is_file():
        if sk.exists():
            sk.rename(DATA / ("skeleton_incomplete_" + job.id))
        sh(job, COMPOSER + ["create-project", "laravel/laravel:^12.0", str(sk), "--no-install", "--no-scripts",
                            "--no-interaction", "--prefer-dist"], cwd=DATA)
    job.state["skel_composer"] = json.loads((sk / "composer.json").read_text())
    job.log("skeleton ready")


def build_composer(job):
    old = job.state["old_composer"]
    new = job.state["skel_composer"]
    relaxed = set(job.state.get("relaxed", []))
    gone = set(job.state.get("dropped", []))
    local = job.state.get("local_pkgs", {})
    req = {"php": "^8.2", "laravel/framework": "^12.0"}
    req.update(new["require"])
    for k, v in old.get("require", {}).items():
        if k in req or k in SKIP or k in gone:
            continue
        if k.startswith(("ext-", "lib-")):
            req[k] = v
        else:
            req[k] = "*" if (k in relaxed or k in local) else v
    req["laravel/ui"] = "*"
    req["laravel/helpers"] = "*"
    psr4 = dict(new["autoload"]["psr-4"])
    psr4.update(old.get("autoload", {}).get("psr-4", {}))
    auto = {"psr-4": psr4}
    files = [f for f in old.get("autoload", {}).get("files", []) if (job.path / f).is_file()]
    if files:
        auto["files"] = files
    cm = [c for c in old.get("autoload", {}).get("classmap", []) if not c.startswith("database") and (job.path / c).exists()]
    if cm:
        auto["classmap"] = cm
    out = {"name": old.get("name", "app/app"), "type": "project", "require": req, "require-dev": new["require-dev"],
           "autoload": auto, "autoload-dev": new["autoload-dev"], "scripts": new["scripts"], "extra": new["extra"],
           "config": new["config"], "minimum-stability": "stable", "prefer-stable": True}
    if local:
        out["repositories"] = [{"type": "path", "url": u, "options": {"symlink": False}} for u in local.values()]
    return out


def s_resolve(job):
    old = job.state["old_composer"]
    skel = job.state["skel_composer"]
    keep = set(skel["require"]) | set(skel.get("require-dev", {})) | {"laravel/ui", "laravel/helpers"}
    job.state["dropped"] = []
    job.state["dropped_ns"] = {}
    job.state["dropped_alias"] = {}
    job.state["relaxed"] = []
    job.state["flagged"] = []
    job.state["local_pkgs"] = {}
    for k in old.get("require", {}):
        if k not in SKIP and DROP.search(k):
            drop_pkg(job, k, "no Laravel 12 version")
    scratch = WORK / job.id
    scratch.mkdir(parents=True, exist_ok=True)
    job.state["scratch"] = str(scratch)
    flaky = {}
    for _ in range(60):
        data = build_composer(job)
        text = json.dumps(data, indent=4)
        (scratch / "composer.json").write_text(text)
        code, out = sh(job, COMPOSER + ["update", "--no-interaction", "--no-scripts", "--no-progress", "--prefer-dist",
                                        "--no-autoloader"], check=False, cwd=scratch)
        if code == 0:
            job.state["composer_out"] = text
            job.log("resolved %d packages in scratch" % len(read_json(scratch / "composer.lock").get("packages", [])))
            return
        roots = set(data["require"]) - keep
        for x in re.findall(r"Root composer\.json requires ([a-z0-9_.\-]+/[a-z0-9_.\-]+)[^\n]*flagged as malware", out):
            if x not in job.state["flagged"]:
                job.state["flagged"].append(x)
                job.log("composer reports %s as malware, it is not reinstalled" % x)
        dl = re.search(r"Failed to download ([a-z0-9_.\-]+/[a-z0-9_.\-]+) from dist", out)
        if dl:
            target = dl.group(1)
            dead = bool(re.search(r"\b404\b|Not Found|could not be found", out))
            flaky[target] = flaky.get(target, 0) + 1
            if not dead and flaky[target] < 2:
                job.log("retrying download of " + target)
                time.sleep(3)
                continue
            lock = read_json(scratch / "composer.lock")
            hit = culprits(lock, sorted(roots), target)
            if not hit:
                raise RuntimeError("cannot download %s and no removable package requires it: %s" % (target, out[-600:]))
            for r in hit:
                drop_pkg(job, r, "download failed: " + target, lock)
            continue
        found = list(dict.fromkeys(x for x in re.findall(r"Root composer\.json requires ([a-z0-9_.\-]+/[a-z0-9_.\-]+)", out) if x in roots))
        if not found:
            raise RuntimeError("composer update failed: " + out[-1200:])
        name = found[0]
        if name not in job.state["relaxed"] and data["require"].get(name) != "*":
            job.state["relaxed"].append(name)
            job.log("relaxed constraint for " + name)
        elif localize(job, name, out):
            continue
        else:
            drop_pkg(job, name, "cannot be installed on Laravel 12 and PHP 8.3")
    raise RuntimeError("composer kept failing after 60 attempts")


def s_rename_old(job):
    for rel in RENAME:
        if job.state.get("nested") and rel in ("index.php", ".htaccess"):
            continue
        move(job, rel)


def split_array(text, start):
    spans, depth, i, begin, q = [], 0, start, start, None
    while i < len(text):
        c = text[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif text.startswith("//", i) or c == "#":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = len(text) if j < 0 else j + 2
            continue
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                spans.append((begin, i))
                return spans, i
            depth -= 1
        elif c == "," and depth == 0:
            spans.append((begin, i + 1))
            begin = i + 1
        i += 1
    return spans, -1


def cfg_parse(text):
    m = re.search(r"return\s*\[", text)
    if not m:
        return [], -1
    spans, end = split_array(text, m.end())
    out = []
    for b, e in spans:
        s0 = LEAD.match(text, b, e).end()
        body = text[s0:e].rstrip()
        km = re.match(r"['\"]([^'\"]+)['\"]\s*=>", body)
        if km:
            out.append((km.group(1), s0, s0 + len(body), body))
    return out, end


def cfg_insert(text, items):
    m = re.search(r"return\s*\[", text)
    _, end = split_array(text, m.end())
    head = text[:end].rstrip()
    if not head.endswith((",", "[")):
        head += ","
    body = "\n".join("    " + (s if s.endswith(",") else s + ",") for s in items)
    return head + "\n\n" + body + "\n" + text[end:]


def scalar(v):
    m = re.match(r"['\"][^'\"]+['\"]\s*=>\s*(.+)$", v.rstrip().rstrip(","), re.S)
    if not m:
        return ""
    s = m.group(1).strip()
    if "\n" in s or "env(" in s or s.startswith(("[", "array")):
        return ""
    return s


def carry_aliases(v):
    out = []
    for name, cls in re.findall(r"['\"](\w+)['\"]\s*=>\s*\\?([\w\\]+)::class", v):
        if cls.startswith("Illuminate\\"):
            continue
        out.append("        '%s' => \\%s::class," % (name, cls))
    return out


def merge_config(job, f, target):
    old, old_end = cfg_parse(f.read_text(errors="replace"))
    if old_end < 0:
        return ["unparsed"]
    text = target.read_text(errors="replace")
    orig = text
    new, new_end = cfg_parse(text)
    if new_end < 0:
        return ["unparsed"]
    cur = {k: (s, e, b) for k, s, e, b in new}
    left, add, swaps = [], [], []
    stream_fix = False
    for k, _, _, v in old:
        if k in cur:
            s, e, b = cur[k]
            sv = scalar(v)
            if sv and sv not in b:
                if "\n" in b:
                    left.append(k)
                else:
                    swaps.append((s, e, v if v.endswith(",") else v + ","))
        elif f.name == "services.php":
            add.append(v)
        elif f.name == "app.php" and k == "aliases":
            lines = carry_aliases(v)
            inner = "[\n" + "\n".join(lines) + "\n    ]" if lines else "[]"
            add.append("'aliases' => \\Illuminate\\Support\\Facades\\Facade::defaultAliases()->merge(%s)->toArray()," % inner)
        elif f.name == "mail.php" and k == "stream":
            if re.search(r"verify_peer['\"]?\s*=>\s*false", v):
                stream_fix = True
        elif k in CFG_OBSOLETE.get(f.name, ()):
            continue
        elif "env(" not in v:
            left.append(k)
    for s, e, v in sorted(swaps, reverse=True):
        text = text[:s] + v + text[e:]
    if add:
        text = cfg_insert(text, add)
    if stream_fix:
        patched = re.sub(r"('smtp'\s*=>\s*\[\s*'transport'\s*=>\s*'smtp',)",
                         lambda m: m.group(1) + "\n            'verify_peer' => false,", text, count=1)
        if patched != text:
            text = patched
            job.log("config/mail.php: smtp verify_peer disabled to match the old stream options")
        else:
            left.append("stream")
    if text != orig:
        target.write_text(text)
        job.log("config/%s: applied %d old values and merged %d old entries" % (f.name, len(swaps), len(add)))
    else:
        job.log("config/%s kept skeleton defaults" % f.name)
    return left


def s_skeleton(job):
    sk = DATA / "skeleton"
    p = job.path
    shutil.copytree(sk / "bootstrap", p / "bootstrap")
    shutil.copytree(sk / "config", p / "config")
    shutil.copy2(sk / "artisan", p / "artisan")
    os.chmod(p / "artisan", 0o755)
    job.manifest.append({"t": "create", "path": "bootstrap"})
    job.manifest.append({"t": "create", "path": "config"})
    job.manifest.append({"t": "create", "path": "artisan"})
    write(job, "public/index.php", (sk / "public" / "index.php").read_text())
    oldcfg = p / ("config_old_" + job.id)
    review = []
    if oldcfg.is_dir():
        for f in sorted(oldcfg.glob("*.php")):
            target = p / "config" / f.name
            if not target.exists():
                shutil.copy2(f, target)
                job.log("carried custom config " + f.name)
                continue
            left = merge_config(job, f, target)
            if left:
                review.append("%s (%s)" % (f.name, ", ".join(left)))
    if review:
        job.warn("review old config values in %s that have no equivalent in the new config: %s" % (oldcfg.name, "; ".join(review)))


def s_composer_json(job):
    write(job, "composer.json", job.state["composer_out"])


def s_composer_install(job):
    scratch = Path(job.state["scratch"])
    if not (scratch / "vendor").is_dir() or not (scratch / "composer.lock").is_file():
        raise RuntimeError("resolved packages are missing from the scratch folder")
    if (job.path / "vendor").exists() or (job.path / "composer.lock").exists():
        raise RuntimeError("vendor or composer.lock still present, old files were not renamed")
    adopt(job, scratch / "vendor", "vendor")
    adopt(job, scratch / "composer.lock", "composer.lock")
    sh(job, COMPOSER + ["dump-autoload", "--no-scripts"])
    if job.state.get("dropped"):
        job.log("removed packages: " + ", ".join(job.state["dropped"]))
    if job.state.get("local_pkgs"):
        job.log("packages kept from the existing install: " + ", ".join(job.state["local_pkgs"]))


def cls_ok(job, c):
    c = c.lstrip("\\")
    if c.startswith("App\\"):
        return (job.path / "app" / (c[4:].replace("\\", "/") + ".php")).exists()
    return c.startswith("Illuminate\\")


def migrate_schedule(job):
    f = job.path / ("app/Console/Kernel.php_old_" + job.id)
    if not f.is_file():
        return
    t = f.read_text(errors="replace")
    m = re.search(r"function\s+schedule\s*\([^)]*\)[^{]*\{", t)
    if not m:
        return
    depth, j = 1, m.end()
    while j < len(t) and depth:
        depth += (t[j] == "{") - (t[j] == "}")
        j += 1
    body = t[m.end():j - 1]
    code = re.sub(r"//[^\n]*|#[^\n]*|/\*.*?\*/", "", body, flags=re.S).strip()
    if not code:
        return
    body = re.sub(r"\$schedule\s*->", "Schedule::", body)
    if depth or "$schedule" in body or "$this" in body:
        job.warn("app/Console/Kernel.php schedule() entries need a manual move to routes/console.php")
        return
    cons = read(job, "routes/console.php") or "<?php\n"
    if "Facades\\Schedule;" not in cons:
        cons = cons.replace("<?php", "<?php\n\nuse Illuminate\\Support\\Facades\\Schedule;", 1)
    write(job, "routes/console.php", cons.rstrip("\n") + "\n\n" + textwrap.dedent(body).strip("\n") + "\n")
    job.log("moved schedule() entries to routes/console.php")


def routes_without_controllers(job):
    base = job.path / "app/Http/Controllers"
    out = []
    for f in sorted((job.path / "routes").glob("*.php")):
        if f.name in ("console.php", "channels.php"):
            continue
        t = f.read_text(errors="replace")
        if "citigate" in t.lower():
            continue
        cs = set(re.findall(r"['\"]((?:\w+\\{1,2})*\w+Controller)(?:@\w+)?['\"]", t))
        if cs and all(not (base / (c.replace("\\\\", "\\").replace("\\", "/") + ".php")).is_file() for c in cs):
            out.append(f.name)
    return out


def php_name(s):
    return str(s).replace("\\\\", "\\").strip().lstrip("\\")


def kernel_uses(ker):
    out = {}
    for fq, alias in re.findall(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;", ker, re.M):
        out[alias or fq.split("\\")[-1]] = fq
    return out


def kernel_ns(ker):
    m = re.search(r"^\s*namespace\s+([\w\\]+)\s*;", ker, re.M)
    return m.group(1) if m else "App\\Http"


def resolve_cls(raw, uses, ns, literal):
    s = str(raw).strip()
    n = php_name(s)
    if s.startswith("\\"):
        return n
    head = n.split("\\")[0]
    if head in uses:
        return uses[head] + n[len(head):]
    if literal and "\\" in n:
        return n
    return ns + "\\" + n


def array_entries(text, pattern):
    m = re.search(pattern, text)
    if not m:
        return []
    spans, end = split_array(text, m.end())
    out = []
    for b, e in spans:
        s0 = LEAD.match(text, b, e).end()
        body = text[s0:e].strip().rstrip(",").strip()
        km = re.match(r"['\"]([^'\"]+)['\"]\s*=>\s*(.+)$", body, re.S)
        if km:
            out.append((km.group(1), km.group(2).strip()))
    return out


def alias_target(v, uses, ns):
    m = re.match(r"([\\\w]+)::class$", v)
    if m:
        return resolve_cls(m.group(1), uses, ns, False)
    m = re.match(r"['\"]([^'\"]+)['\"]$", v)
    if m and re.match(r"^[\\\w]+$", php_name(m.group(1))):
        return resolve_cls(m.group(1), uses, ns, True)
    return ""


def classes_exist(job, names):
    names = sorted(set(n for n in names if n))
    if not names:
        return {}
    b64 = base64.b64encode(json.dumps(names).encode()).decode()
    code = ("require 'vendor/autoload.php';$o=[];"
            "foreach(json_decode(base64_decode('%s'),true) as $c){$o[$c]=class_exists($c);}"
            "echo 'CLASSES'.json_encode($o);") % b64
    rc, out = sh(job, [PHP, "-r", code], check=False, timeout=300)
    m = re.search(r"CLASSES(\{.*\})", out)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def used_middleware(job, skip):
    texts = []
    for f in sorted((job.path / "routes").glob("*.php")):
        if f.name in ("console.php", "channels.php") or f.name in skip:
            continue
        texts.append(f.read_text(errors="replace"))
    cdir = job.path / "app/Http/Controllers"
    if cdir.is_dir():
        for f in cdir.rglob("*.php"):
            try:
                texts.append(f.read_text(errors="replace"))
            except OSError:
                continue
    names = set()

    def collect(cap):
        for s in re.findall(r"['\"]([^'\"]+)['\"]", cap):
            n = s.split(":")[0].strip()
            if re.match(r"^[A-Za-z][\w.\-]*$", n):
                names.add(n)

    for t in texts:
        for cap in re.findall(r"middleware\(\s*(\[.*?\]|'[^']*'|\"[^\"]*\")", t, re.S):
            collect(cap)
        for cap in re.findall(r"['\"]middleware['\"]\s*=>\s*(\[.*?\]|'[^']*'|\"[^\"]*\")", t, re.S):
            collect(cap)
    return names


def guess_middleware(job, name):
    base = job.path / "app/Http/Middleware"
    if not base.is_dir():
        return ""
    key = "".join(re.split(r"[^A-Za-z0-9]+", name)).lower()
    if not key:
        return ""
    hits = [f for f in base.rglob("*.php") if f.stem.lower() in (key, key + "middleware")]
    if len(hits) != 1:
        return ""
    rel = hits[0].relative_to(job.path / "app").with_suffix("").as_posix().replace("/", "\\")
    return "App\\" + rel


def s_bootstrap_app(job):
    p = job.path
    migrate_schedule(job)
    ker = job.state.get("old_kernel", "")
    files = sorted(f.name for f in (p / "routes").glob("*.php"))
    skip = routes_without_controllers(job) if job.opts.get("skip_missing", True) else []
    job.state["skipped_routes"] = skip
    if skip:
        job.note("route files skipped because every controller they use is missing from this project (the old site could not serve them either): " + ", ".join(skip))
    rows = []
    for f in files:
        if f in ("console.php", "channels.php") or f in skip:
            continue
        rows.append("['%s', '%s', '%s']" % (f, "api" if f == "api.php" else "web", "api" if f == "api.php" else ""))
    uses = kernel_uses(ker)
    ns = kernel_ns(ker)
    raw = {}
    for k, v in array_entries(ker, r"\$(?:routeMiddleware|middlewareAliases)\s*=\s*\["):
        c = alias_target(v, uses, ns)
        if c:
            raw[k] = c
    web_c = []
    m = re.search(r"['\"]web['\"]\s*=>\s*\[(.*?)\]", ker, re.S)
    if m:
        for c in re.findall(r"([\\\w]+)::class", m.group(1)):
            fq = resolve_cls(c, uses, ns, False)
            if fq.startswith("App\\") and fq.split("\\")[-1] not in STD:
                web_c.append(fq)
    glob_c = []
    m = re.search(r"\$middleware\s*=\s*\[(.*?)\];", ker, re.S)
    if m:
        for c in re.findall(r"([\\\w]+)::class", m.group(1)):
            fq = resolve_cls(c, uses, ns, False)
            if fq.startswith("App\\") and fq.split("\\")[-1] not in STD:
                glob_c.append(fq)
    groups = {}
    for gk, gv in array_entries(ker, r"\$middlewareGroups\s*=\s*\["):
        if gk in ("web", "api") or not re.match(r"^[\w.\-]+$", gk):
            continue
        items = []
        for cm, sm in re.findall(r"([\\\w]+)::class|['\"]([^'\"]+)['\"]", gv):
            if cm:
                items.append(("c", resolve_cls(cm, uses, ns, False)))
            elif re.match(r"^[\w.\-:,|]+$", sm):
                items.append(("s", sm))
        groups[gk] = items
    used = used_middleware(job, skip)
    names = set(raw.values()) | set(web_c) | set(glob_c) | {c for items in groups.values() for t, c in items if t == "c"}
    ex = classes_exist(job, names)

    def ok(c):
        if ex is not None:
            return bool(ex.get(c))
        return cls_ok(job, c)

    aliases = {k: "\\" + c for k, c in raw.items() if ok(c)}
    group_names = set(groups) | {"web", "api"}
    unresolved = []
    for n in sorted(used):
        if n in aliases or n in MW_BUILTIN or n in group_names:
            continue
        g = guess_middleware(job, n)
        if g:
            aliases[n] = "\\" + g
            job.note("route middleware '%s' is used by the routes but was not registered by the old kernel, so it was mapped by name to %s" % (n, g))
        else:
            unresolved.append(n)
    if unresolved:
        job.warn("route middleware used in routes or controllers but no class could be found for it, requests that use it fail with Target class does not exist: " + ", ".join(unresolved))
    job.state["aliases"] = sorted(aliases)
    web = ["\\" + c for c in web_c if ok(c)]
    glob = ["\\" + c for c in glob_c if ok(c)]
    mw = ["        $middleware->trustProxies(at: '*');"]
    if aliases:
        mw.append("        $middleware->alias([\n" + "".join("            '%s' => %s::class,\n" % kv for kv in aliases.items()) + "        ]);")
    for gk, items in groups.items():
        parts = []
        for t, c in items:
            if t == "c" and ok(c):
                parts.append("\\" + c + "::class")
            elif t == "s":
                parts.append("'%s'" % c)
        mw.append("        $middleware->group('%s', [%s]);" % (gk, ", ".join(parts)))
    if web:
        mw.append("        $middleware->web(append: [" + ", ".join(c + "::class" for c in web) + "]);")
    if glob:
        mw.append("        $middleware->append([" + ", ".join(c + "::class" for c in glob) + "]);")
    if (p / "app/Http/Middleware/VerifyCsrfToken.php").exists():
        mw.append("        $middleware->web(replace: [\\Illuminate\\Foundation\\Http\\Middleware\\ValidateCsrfToken::class => \\App\\Http\\Middleware\\VerifyCsrfToken::class]);")
    exc = csrf_exceptions(job)
    if exc:
        mw.append("        $middleware->validateCsrfTokens(except: [%s]);" % ", ".join("'%s'" % x for x in exc))
    cmd = "commands: __DIR__.'/../routes/console.php',\n        " if (p / "routes/console.php").exists() else ""
    php = """<?php

use Illuminate\\Foundation\\Application;
use Illuminate\\Foundation\\Configuration\\Exceptions;
use Illuminate\\Foundation\\Configuration\\Middleware;
use Illuminate\\Support\\Facades\\Route;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        %shealth: '/up',
        then: function () {
            foreach ([%s] as [$file, $group, $prefix]) {
                Route::middleware($group)->prefix($prefix)->namespace('App\\Http\\Controllers')->group(base_path('routes/'.$file));
            }
        },
    )
    ->withMiddleware(function (Middleware $middleware): void {
%s
    })
    ->withExceptions(function (Exceptions $exceptions): void {
    })
    ->create();
""" % (cmd, ", ".join(rows), "\n".join(mw))
    write(job, "bootstrap/app.php", php)
    if not job.opts.get("repair") or not (p / "bootstrap/providers.php").exists():
        write(job, "bootstrap/providers.php", "<?php\n\nreturn [\n    App\\Providers\\AppServiceProvider::class,\n];\n")
    for needed in ("AppServiceProvider",):
        if not (p / "app/Providers" / (needed + ".php")).exists():
            write(job, "app/Providers/%s.php" % needed,
                  "<?php\n\nnamespace App\\Providers;\n\nuse Illuminate\\Support\\ServiceProvider;\n\nclass AppServiceProvider extends ServiceProvider\n{\n    public function register(): void\n    {\n    }\n\n    public function boot(): void\n    {\n    }\n}\n")
    if job.opts.get("app_url"):
        asp = read(job, "app/Providers/AppServiceProvider.php")
        lines = []
        if "defaultStringLength" not in asp:
            lines.append("        \\Illuminate\\Support\\Facades\\Schema::defaultStringLength(191);")
        if "forceRootUrl" not in asp:
            lines.append("        \\Illuminate\\Support\\Facades\\URL::forceRootUrl(config('app.url'));")
        if "forceScheme" not in asp:
            lines.append("        if (app()->environment('production')) {\n            \\Illuminate\\Support\\Facades\\URL::forceScheme('https');\n        }")
        if lines and "public function boot" in asp:
            asp = re.sub(r"(public function boot\(\)[^{]*\{)", lambda m: m.group(1) + "\n" + "\n".join(lines), asp, count=1)
            write(job, "app/Providers/AppServiceProvider.php", asp)


def s_auth_model(job):
    cfg = read(job, "config/auth.php")
    model = job.state["user_model"]
    new = re.sub(r"(['\"]model['\"]\s*=>\s*)env\([^)]*\)", lambda m: m.group(1) + model + "::class", cfg)
    new = re.sub(r"^use App\\Models\\User;\n", "", new, flags=re.M)
    write(job, "config/auth.php", new)
    job.log("auth model " + model)


def s_env(job):
    env = read(job, ".env")
    if not env:
        job.warn(".env not found, skipped")
        return
    keys = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", env, flags=re.M))
    add = []
    if "CACHE_STORE" not in keys:
        add.append("CACHE_STORE=" + keys.get("CACHE_DRIVER", "file"))
    if "SESSION_DRIVER" not in keys:
        add.append("SESSION_DRIVER=file")
    if "QUEUE_CONNECTION" not in keys:
        add.append("QUEUE_CONNECTION=sync")
    if "MAIL_MAILER" not in keys:
        add.append("MAIL_MAILER=" + keys.get("MAIL_DRIVER", "smtp"))
    env = re.sub(r"^(AUTH_MODEL|ASSET_URL)=.*\n?", "", env, flags=re.M)
    if job.opts.get("app_url"):
        u = job.opts["app_url"].rstrip("/")
        env = re.sub(r"^APP_URL=.*$", 'APP_URL="%s"' % u, env, flags=re.M) if "APP_URL" in keys else env + '\nAPP_URL="%s"' % u
    if add:
        env = env.rstrip("\n") + "\n" + "\n".join(add) + "\n"
    write(job, ".env", env)
    job.log("env keys added: " + ", ".join(a.split("=")[0] for a in add))


def s_session_guard(job):
    n = 0
    pat = re.compile(r"(?<![\w>:])session_start\(\);")
    for f in (job.path / "app").rglob("*.php"):
        t = f.read_text(errors="replace")
        if "session_start()" in t and "PHP_SESSION_NONE" not in t:
            new = pat.sub("if (session_status() === PHP_SESSION_NONE) { session_start(); }", t)
            if new != t:
                write(job, str(f.relative_to(job.path)), new)
                n += 1
    job.log("session_start guarded in %d files" % n)


def s_route_home(job):
    home = "/home"
    for f in sorted((job.path / "app/Providers").glob("RouteServiceProvider*")):
        if f.is_file():
            m = re.search(r"const\s+HOME\s*=\s*['\"]([^'\"]+)['\"]", f.read_text(errors="replace"))
            if m:
                home = m.group(1)
                break
    pat = re.compile(r"(?:\\?App\\Providers\\)?RouteServiceProvider::HOME\b")
    n = 0
    for base in ("app", "routes"):
        for f in (job.path / base).rglob("*.php"):
            t = f.read_text(errors="replace")
            if "RouteServiceProvider::HOME" in t:
                new = pat.sub("'%s'" % home, t)
                if new != t:
                    write(job, str(f.relative_to(job.path)), new)
                    n += 1
    job.log("RouteServiceProvider::HOME replaced with '%s' in %d files" % (home, n))


def s_web_files(job):
    if job.state.get("nested"):
        job.log("web files skipped, %s keeps its own index.php and .htaccess" % job.path.parent)
        return
    o = job.opts
    host, base = o.get("staging_host", "").strip(), o.get("staging_base", "").strip().rstrip("/")
    if host and base:
        head = "$base = (($_SERVER['HTTP_HOST'] ?? '') === '%s') ? '%s' : '';\n$_SERVER['SCRIPT_NAME'] = $base.'/index.php';\n$_SERVER['PHP_SELF'] = $base.'/index.php';\n$_SERVER['SCRIPT_FILENAME'] = __FILE__;\n\n" % (host, base)
    else:
        head = ""
    idx = """<?php

use Illuminate\\Http\\Request;

define('LARAVEL_START', microtime(true));

%sif (file_exists($maintenance = __DIR__.'/storage/framework/maintenance.php')) {
    require $maintenance;
}

require __DIR__.'/vendor/autoload.php';

(require_once __DIR__.'/bootstrap/app.php')
    ->handleRequest(Request::capture());
""" % head
    write(job, "index.php", idx)
    ht = """<IfModule mod_rewrite.c>
<IfModule mod_negotiation.c>
Options -MultiViews -Indexes
</IfModule>
RewriteEngine On
RewriteCond %{HTTP:Authorization} .
RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]
RewriteRule ^(app|bootstrap|config|database|routes|storage|tests|vendor|packages_local)(_old_[^/]*)?/ - [F,L]
RewriteRule ^[^/]*(_old_|\\.bak-) - [F,L]
RewriteRule ^[^/]+\\.(sql|zip|log|bak)$ - [F,L]
RewriteRule ^(dbbackup|error_log|artisan|server\\.php|composer\\.(json|lock)|package\\.json|yarn\\.lock|php\\.ini|\\.user\\.ini|web\\.config|phpunit\\.xml) - [F,L]
RewriteRule ^vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php$ - [F]
RewriteCond $1 !^index[.]php$
RewriteCond %{REQUEST_URI}::$1 ^(.*?/)(.*)::\\2$
RewriteCond %{DOCUMENT_ROOT}%1public/$1 -f
RewriteRule ^(.+)$ public/$1 [L]
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{REQUEST_URI} (.+)/$
RewriteRule ^ %1 [L,R=301]
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{REQUEST_FILENAME} !-f
RewriteRule ^ index.php [L]
</IfModule>
<IfModule mod_headers.c>
Header always edit Set-Cookie ^(.*)$ $1;SameSite=None;Secure
</IfModule>
Options -Indexes
<Files .env>
Order allow,deny
Deny from all
</Files>
"""
    write(job, ".htaccess", ht)
    for z in ("frontend.zip", "css.zip", "img.zip"):
        if (job.path / "public" / z).exists():
            job.warn("public/%s is downloadable, delete it after checking" % z)


def s_permissions(job):
    for d in ("storage/logs", "storage/framework/cache/data", "storage/framework/sessions", "storage/framework/views", "bootstrap/cache"):
        (job.path / d).mkdir(parents=True, exist_ok=True)
    for top in ("storage", "bootstrap/cache"):
        for r, ds, fs in os.walk(job.path / top):
            try:
                os.chmod(r, 0o775)
            except OSError:
                pass
            for f in fs:
                try:
                    os.chmod(os.path.join(r, f), 0o664)
                except OSError:
                    pass
    job.log("storage and bootstrap/cache set to 775/664")


def ai_request(payload):
    data = json.dumps(payload).encode()
    headers = {"x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(AI_URL, data=data, headers=headers), timeout=180) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 529) and i < 2:
                time.sleep(10 * (i + 1))
                continue
            raise RuntimeError("Claude API %s: %s" % (e.code, body))
        except (urllib.error.URLError, TimeoutError) as e:
            if i < 2:
                time.sleep(10 * (i + 1))
                continue
            raise RuntimeError("Claude API unreachable: %s" % e)


def newest_log(job):
    logs = sorted((job.path / "storage/logs").glob("*.log"), key=lambda f: f.stat().st_mtime)
    return logs[-1] if logs else None


def log_mark(job):
    f = newest_log(job)
    return (f, f.stat().st_size) if f else (None, 0)


def log_since(job, mark):
    f = newest_log(job)
    if not f:
        return ""
    size = f.stat().st_size
    off = mark[1] if mark[0] == f and mark[1] <= size else 0
    with open(f, "rb") as h:
        h.seek(off)
        data = h.read().decode("utf-8", "replace")
    m = re.search(r"\] \w+\.ERROR: ", data)
    return data[m.start():] if m else ""


def missing_controllers(job):
    base = job.path / "app/Http/Controllers"
    skipped = set(job.state.get("skipped_routes", []))
    found = {}
    for f in sorted((job.path / "routes").glob("*.php")):
        if f.name in skipped:
            continue
        t = f.read_text(errors="replace")
        for c in re.findall(r"['\"]((?:\w+\\{1,2})*\w+Controller)(?:@\w+)?['\"]", t):
            found.setdefault(c.replace("\\\\", "\\"), set()).add(f.name)
    return {c: sorted(fs) for c, fs in found.items() if not (base / (c.replace("\\", "/") + ".php")).is_file()}


def class_dir_files(job, fq):
    short = fq.split("\\")[-1]
    app = job.path / "app"
    out = []
    if not app.is_dir():
        return out
    for f in app.rglob(short + ".php"):
        t = f.read_text(errors="replace")
        m = re.search(r"^\s*namespace\s+([\w\\]+)\s*;", t, re.M)
        if m and m.group(1) + "\\" + short == fq:
            out.append(str(f.relative_to(job.path)))
    return out[:3]


def route_gaps(job, out):
    gone = missing_controllers(job)
    absent, moved = {}, {}
    for c, fs in gone.items():
        hit = class_dir_files(job, "App\\Http\\Controllers\\" + c)
        if hit:
            moved[c] = hit
        else:
            for f in fs:
                absent.setdefault(f, []).append(c)
    named = {"App\\Http\\Controllers\\" + c for c in gone}
    plain = {}
    route_files = sorted((job.path / "routes").glob("*.php"))
    for x in sorted(set(re.findall(r'Class "?\\?([\w\\]+)"? (?:not found|does not exist)', out))):
        if x in named:
            continue
        short = x.split("\\")[-1]
        plain[x] = [f.name for f in route_files if short in f.read_text(errors="replace")]
    return absent, moved, plain


def ai_error(job):
    code, out = sh(job, [PHP, "artisan", "route:list"], check=False)
    if code != 0:
        m = re.search(r'Class "?\\?([\w\\]+)"? does not exist', out)
        known = {"App\\Http\\Controllers\\" + c for c in missing_controllers(job)}
        if not (m and m.group(1) in known) and ai_target(job, out)[0]:
            return out
    mark = log_mark(job)
    res = probe(job, probe_paths(job))
    for pth, s in res.items():
        if isinstance(s, int) and s >= 500:
            err = log_since(job, mark) or "%s returned %s" % (pth, s)
            if ENV_ERR.search(err):
                continue
            return err
    for u in [x for x in job.opts.get("check_urls", "").split(",") if x]:
        mark = log_mark(job)
        s = http_status(u)
        if isinstance(s, int) and s >= 500:
            err = log_since(job, mark) or "%s returned %s" % (u, s)
            if ENV_ERR.search(err):
                continue
            return err
    return ""


def ai_target(job, text):
    root = job.path.resolve()
    for m in re.finditer(r"\bat\s+(\S+?\.php)(?:[:(](\d+))?", text):
        try:
            rel = (root / m.group(1)).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel.startswith(AI_ALLOWED) and (root / rel).is_file():
            return rel, int(m.group(2) or 0)
    for m in re.finditer(r"(?<![\w/.\-])((?:app|routes|resources/views)/[^\s:()'\"\[\],]+\.php)(?:\((\d+)\)|:(\d+))?", text):
        if (root / m.group(1)).is_file():
            return m.group(1), int(m.group(2) or m.group(3) or 0)
    for m in re.finditer(r"(/[^\s:()'\"\[\],]+\.php)(?:\((\d+)\)|:(\d+))?", text):
        try:
            rel = Path(m.group(1)).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel.startswith(AI_ALLOWED) and (root / rel).is_file():
            return rel, int(m.group(2) or m.group(3) or 0)
    for c in re.findall(r"App(?:\\{1,2}\w+)+", text):
        short = re.split(r"\\+", c)[-1]
        for f in sorted((root / "routes").glob("*.php")):
            if short in f.read_text(errors="replace"):
                return "routes/" + f.name, 0
    return None, 0


def ai_ask(job, rel, line, err, note):
    text = read(job, rel)
    if len(text) > AI_MAX_FILE:
        rows = text.splitlines()
        a = max(0, line - 200)
        body = "\n".join(rows[a:line + 200])
        part = "lines %d to %d only; each old string must still be unique in the whole file" % (a + 1, min(len(rows), line + 200))
    else:
        body = text
        part = "full file"
    prompt = "Migrated from Laravel %s to Laravel 12.\nRemoved packages with no Laravel 12 version: %s\nFile: %s (%s)\n\nError:\n%s\n\n%sFile content:\n%s" % (
        job.state.get("from_version") or "unknown", ", ".join(job.state.get("dropped", [])) or "none", rel, part, err[:3500],
        ("Previous attempt was rejected: %s\n\n" % note) if note else "", body)
    data = ai_request({"model": AI_MODEL, "max_tokens": 4096, "system": AI_SYSTEM, "tools": [AI_TOOL],
                       "messages": [{"role": "user", "content": prompt}]})
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if not block:
        raise RuntimeError("Claude returned no fix")
    u = data.get("usage", {})
    return block["input"], u.get("input_tokens", 0) + u.get("output_tokens", 0)


def ai_apply(text, edits):
    for i, e in enumerate(edits, 1):
        old, new = e.get("old", ""), e.get("new", "")
        if not old or text.count(old) != 1:
            raise ValueError("edit %d: old text not found exactly once" % i)
        text = text.replace(old, new, 1)
    return text


def ai_guard(rel, edits):
    if "citigate" in rel.lower():
        return "citigate files are never edited"
    for e in edits:
        old, new = e.get("old", ""), e.get("new", "")
        if "citigate" in old.lower():
            return "code that mentions citigate is never edited"
        if old.strip() and not new.strip():
            return "deleting code is not allowed, replace it with a working equivalent or return no edits"
    return ""


def php_lint(text):
    with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as f:
        f.write(text)
    try:
        r = subprocess.run([PHP, "-l", f.name], capture_output=True, text=True, env=envx())
    finally:
        os.unlink(f.name)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def s_ai_fix(job):
    if not api_key():
        job.warn("AI fix skipped: no Claude API key")
        return
    sh(job, [PHP, "artisan", "package:discover", "--ansi"], check=False)
    sh(job, [PHP, "artisan", "optimize:clear"], check=False)
    calls, tokens, note = 0, 0, ""
    attempts = {}
    while calls < AI_MAX_CALLS and tokens < AI_TOKEN_BUDGET:
        err = ai_error(job)
        if not err:
            job.log("AI fix: no errors left (%d calls, %d tokens)" % (calls, tokens))
            job.state["ai_tokens"] = tokens
            return
        rel, line = ai_target(job, err)
        if not rel:
            job.warn("AI fix: no editable file found for error: " + err[:300].replace("\n", " "))
            break
        if "citigate" in rel.lower():
            job.warn("AI fix: %s belongs to citigate and is never edited by the tool, check it manually: %s" % (rel, err[:200].replace("\n", " ")))
            break
        attempts[rel] = attempts.get(rel, 0) + 1
        if attempts[rel] > 3:
            job.warn("AI fix: gave up on %s after 3 attempts" % rel)
            break
        calls += 1
        try:
            fix, used = ai_ask(job, rel, line, err, note)
        except Exception as e:
            job.warn("AI fix stopped: %s" % e)
            break
        tokens += used
        summary = str(fix.get("summary", ""))[:300]
        edits = fix.get("edits") or []
        if not edits:
            job.warn("AI fix: no change made to %s: %s" % (rel, summary))
            break
        try:
            guard = ai_guard(rel, edits)
            if guard:
                raise ValueError(guard)
            new = ai_apply(read(job, rel), edits)
            if not rel.endswith(".blade.php"):
                ok, msg = php_lint(new)
                if not ok:
                    raise ValueError("php -l failed: " + msg[:300])
        except ValueError as e:
            note = str(e)
            job.log("AI edit rejected for %s: %s" % (rel, note))
            continue
        note = ""
        write(job, rel, new)
        job.note("AI edited %s (backup kept): %s" % (rel, summary))
    job.state["ai_tokens"] = tokens
    left = ai_error(job)
    if left:
        job.warn("AI fix finished with errors left: " + left[:300].replace("\n", " "))


def s_verify(job):
    sh(job, [PHP, "artisan", "package:discover", "--ansi"], check=False)
    sh(job, [PHP, "artisan", "optimize:clear"], check=False)
    code, out = sh(job, [PHP, "artisan", "--version"], check=False)
    if code != 0:
        raise RuntimeError("health gate: application does not boot: " + out[-500:])
    code, out = sh(job, [PHP, "artisan", "route:list"], check=False)
    if code != 0:
        absent, moved, plain = route_gaps(job, out)
        for f, cs in sorted(absent.items()):
            job.log("missing controller files for routes/%s: %s" % (f, ", ".join(sorted(cs))))
        if absent:
            job.note("controller files that do not exist anywhere in app/ (the old site lacked them too), route:list stays unavailable until they are restored or the routes removed: " + "; ".join(
                "routes/%s: %d (%s%s)" % (f, len(cs), ", ".join(sorted(cs)[:3]), ", ..." if len(cs) > 3 else "")
                for f, cs in sorted(absent.items())))
        for c, hits in sorted(moved.items()):
            job.warn("%s is declared in %s but that path does not match its namespace, move the file to app/Http/Controllers/%s.php" % (c, ", ".join(hits), c.replace("\\", "/")))
        for x, rfs in sorted(plain.items()):
            if x.startswith("App\\"):
                target = job.path / "app" / (x[4:].replace("\\", "/") + ".php")
            else:
                target = job.path / "app/Http/Controllers" / (x.replace("\\", "/") + ".php")
            where = ", ".join("routes/" + n for n in rfs[:4]) or "a route file"
            if target.is_file():
                job.warn("%s exists but %s register it without the controller namespace" % (x, where))
            else:
                job.note("%s is used by %s but the class does not exist, the old site had the same gap" % (x, where))
        if not (absent or moved or plain):
            job.warn("route:list failed: " + out[-300:])
    paths = probe_paths(job)
    mark = log_mark(job)
    res = probe(job, paths)
    for pth in paths:
        job.log("PROBE %s -> %s" % (pth, res.get(pth, "no result")))
    home = res.get(paths[0])
    if not res:
        job.warn("in-process probe produced no result")
    elif isinstance(home, int) and home >= 500:
        err = log_since(job, mark) or "home returned %s" % home
        if ENV_ERR.search(err):
            job.warn("%s returned %s because of a database or environment error: %s" % (paths[0], home, err[:250].replace("\n", " ")))
        else:
            raise RuntimeError("health gate: %s returned %s: %s" % (paths[0], home, err[:700]))
    for pth in paths[1:]:
        s = res.get(pth)
        if isinstance(s, int) and s >= 500:
            job.warn("%s returned %s" % (pth, s))
    for u in [x.strip() for x in job.opts.get("check_urls", "").split(",") if x.strip()]:
        s = http_status(u)
        job.log("GET %s -> %s" % (u, s))
        if s != 200:
            job.warn("%s returned %s" % (u, s))
    dropped = job.state.get("dropped", [])
    if dropped:
        files = code_files(job.path)
        inuse = gateways_in_use(job)
        flagged = set(job.state.get("flagged", []))
        used, idle, unknown = {}, [], []
        for k in dropped:
            nss = job.state.get("dropped_ns", {}).get(k, [])
            als = job.state.get("dropped_alias", {}).get(k, [])
            unused_gw = bool(inuse) and bool(DROP.search(k)) and not any(i in k.lower() for i in inuse)
            if not nss and not als:
                (idle if unused_gw else unknown).append(k)
                continue
            hits = [p for p, t in files.items() if any(ns_used(t, n) for n in nss) or any(alias_used(t, a) for a in als)]
            if hits and unused_gw:
                idle.append(k)
            elif hits:
                used[k] = hits
        if used:
            job.warn("removed packages still referenced in code, replace or remove those parts: " + "; ".join(
                "%s%s (%s%s)" % (k, " [reported as malware]" if k in flagged else "", ", ".join(v[:3]), ", ..." if len(v) > 3 else "")
                for k, v in sorted(used.items())))
        if idle:
            job.log("gateway packages without a Laravel 12 version were taken out of composer only, no code was changed or deleted: " + ", ".join(idle))
        if unknown:
            job.warn("removed packages whose usage could not be checked: " + ", ".join(unknown))
        mal = [k for k in dropped if k in flagged and k not in used]
        if mal:
            job.note("composer reported these packages as malware and they were not reinstalled, no code uses them any more: " + ", ".join(mal))
        clean = [k for k in dropped if k not in used and k not in unknown and k not in idle]
        if clean:
            job.log("removed packages not referenced in code: " + ", ".join(clean))
    if has_citigate(job.path):
        job.log("citigate detected: callback paths exempt from CSRF: " + ", ".join(CITIGATE_PATHS))
        job.log("citigate route files are never skipped and session_start is guarded")
        job.note("citigate gateway was kept: no citigate file was removed, its route files were not skipped, and its callback paths are exempt from CSRF (%s)" % ", ".join(CITIGATE_PATHS))
    mods = [a for a in job.manifest if a.get("t") == "modify"]
    renames = [a for a in job.manifest if a.get("t") == "rename"]
    if mods or renames:
        job.note("no site file was deleted: %d old framework files were renamed with the suffix _old_%s and %d edited files have a .bak-%s copy next to them" % (
            len(renames), job.id, len(mods), job.id))
    job.log("untested by tool: payments, admin, uploads, scheduler, extra auth guards, old class aliases")


def patched_map():
    return read_json(PATCHED)


def save_patched(m):
    tmp = PATCHED.with_name(PATCHED.name + ".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    os.replace(tmp, PATCHED)


def unmark_patched(job):
    m = patched_map()
    if (m.get(job.name) or {}).get("job") == job.id:
        del m[job.name]
        save_patched(m)
        job.log("patched mark removed for " + job.name)


def route_fq(name):
    n = php_name(name)
    return n if n.startswith("App\\") else "App\\Http\\Controllers\\" + n


def add_uses(text, names):
    block = "".join("use %s;\n" % x for x in names)
    found = list(re.finditer(r"^use\s+[\\\w][^;(]*;[ \t]*\r?\n", text, re.M))
    if found:
        pos = found[-1].end()
        return text[:pos] + block + text[pos:]
    m = re.match(r"<\?php[ \t]*\r?\n", text)
    if not m:
        return text
    return text[:m.end()] + "\n" + block + text[m.end():]


def upgrade_routes(text):
    if re.search(r"['\"]namespace['\"]\s*=>|->namespace\(", text):
        return text, "uses a route group namespace, convert its controller strings by hand"
    existing = kernel_uses(text)
    in_ns = re.search(r"^\s*namespace\s+[\w\\]+\s*;", text, re.M) is not None

    def skip(m):
        return "citigate" in m.group(0).lower()

    shorts = {}
    for m in ROUTE_ACTION.finditer(text):
        if not skip(m):
            fq = route_fq(m.group(2))
            shorts.setdefault(fq.split("\\")[-1], set()).add(fq)
    for m in ROUTE_RESOURCE.finditer(text):
        if not skip(m):
            fq = route_fq(m.group(3))
            shorts.setdefault(fq.split("\\")[-1], set()).add(fq)
    imports = set()

    def ref(fq):
        short = fq.split("\\")[-1]
        if not in_ns and existing.get(short) == fq:
            return short
        if in_ns or short in existing or len(shorts[short]) > 1:
            return "\\" + fq
        imports.add(fq)
        return short

    def act(m):
        if skip(m):
            return m.group(0)
        return "[%s::class, '%s']" % (ref(route_fq(m.group(2))), m.group(3))

    def res(m):
        if skip(m):
            return m.group(0)
        return m.group(1) + ref(route_fq(m.group(3))) + "::class"

    new = ROUTE_RESOURCE.sub(res, ROUTE_ACTION.sub(act, text))
    if not in_ns:
        for alias in ("Route", "Auth"):
            if alias not in existing and re.search(r"(?<![\w\\$>:])%s::" % alias, new):
                imports.add("Illuminate\\Support\\Facades\\" + alias)
    names = sorted(x for x in imports if ("use %s;" % x) not in new)
    if names:
        new = add_uses(new, names)
    return new, ""


def patch_file(job, rel, text):
    if write(job, rel, text):
        job.state.setdefault("patch_files", []).append(rel)
    else:
        job.log(rel + " is already up to date")


def s_patch_check(job):
    p = job.path
    if not is_laravel(p):
        raise RuntimeError("composer.json missing or does not require laravel/framework: not a Laravel project")
    if installed_major(p) < 12:
        raise RuntimeError("the site is not on Laravel 12 yet, run the migration first")
    check_access(job)
    job.state["from_version"] = laravel_version(p)
    job.state["citigate"] = has_citigate(p)
    job.state["patch_files"] = []
    job.log("laravel version: %s, mode: patch, citigate: %s" % (job.state["from_version"] or "unknown", job.state["citigate"]))


def s_patch_handler(job):
    patch_file(job, "app/Exceptions/Handler.php", HANDLER_PHP)


def s_patch_controller(job):
    patch_file(job, "app/Http/Controllers/Controller.php", CONTROLLER_PHP)


def s_patch_routes(job):
    changed = 0
    for f in sorted((job.path / "routes").glob("*.php")):
        if f.name in ("console.php", "channels.php"):
            continue
        rel = "routes/" + f.name
        text = f.read_text(errors="replace")
        new, why = upgrade_routes(text)
        if why:
            job.warn("%s %s" % (rel, why))
            continue
        if new == text:
            job.log(rel + " is already up to date")
            continue
        ok, msg = php_lint(new)
        if not ok:
            job.warn("%s was not changed because the upgraded file failed php -l: %s" % (rel, msg[:200]))
            continue
        if write(job, rel, new):
            job.state.setdefault("patch_files", []).append(rel)
            changed += 1
    job.log("route files upgraded: %d" % changed)


def patch_probe(job):
    paths = probe_paths(job)
    mark = log_mark(job)
    res = probe(job, paths)
    for pth in paths:
        job.log("PROBE %s -> %s" % (pth, res.get(pth, "no result")))
    home = res.get(paths[0])
    if not res:
        job.warn("in-process probe produced no result")
    elif isinstance(home, int) and home >= 500:
        err = log_since(job, mark) or "home returned %s" % home
        if ENV_ERR.search(err):
            job.warn("%s returned %s because of a database or environment error: %s" % (paths[0], home, err[:250].replace("\n", " ")))
        else:
            raise RuntimeError("health gate: %s returned %s: %s" % (paths[0], home, err[:700]))
    for pth in paths[1:]:
        s = res.get(pth)
        if isinstance(s, int) and s >= 500:
            job.warn("%s returned %s" % (pth, s))
    for u in [x.strip() for x in job.opts.get("check_urls", "").split(",") if x.strip()]:
        s = http_status(u)
        job.log("GET %s -> %s" % (u, s))
        if s != 200:
            job.warn("%s returned %s" % (u, s))


def s_patch_verify(job):
    sh(job, [PHP, "artisan", "optimize:clear"], check=False)
    code, out = sh(job, [PHP, "artisan", "--version"], check=False)
    if code != 0:
        raise RuntimeError("health gate: application does not boot: " + out[-500:])
    code, out = sh(job, [PHP, "artisan", "route:list"], check=False)
    if code != 0:
        absent, moved, plain = route_gaps(job, out)
        if absent or moved or plain:
            job.note("route:list is unavailable because some controller classes are missing or misplaced, this was already the case before the patch")
        else:
            job.warn("route:list failed: " + out[-300:])
    patch_probe(job)


def s_patch_mark(job):
    m = patched_map()
    m[job.name] = {"job": job.id, "time": time.strftime("%F %T"), "files": job.state.get("patch_files", [])}
    save_patched(m)
    job.log("site marked as patched: " + job.name)


STEPS = [
    ("preflight", "Preflight checks", s_preflight),
    ("skelprep", "Prepare Laravel 12 skeleton", s_skelprep),
    ("resolve", "Resolve and download packages", s_resolve),
    ("rename", "Rename old framework files", s_rename_old),
    ("skeleton", "Install Laravel 12 skeleton", s_skeleton),
    ("composerjson", "Rewrite composer.json", s_composer_json),
    ("composer", "Install packages", s_composer_install),
    ("bootstrap", "Generate bootstrap/app.php", s_bootstrap_app),
    ("auth", "Fix auth user model", s_auth_model),
    ("env", "Patch .env", s_env),
    ("session", "Guard session_start()", s_session_guard),
    ("home", "Replace RouteServiceProvider::HOME", s_route_home),
    ("web", "Write index.php and .htaccess", s_web_files),
    ("perms", "Permissions", s_permissions),
    ("aifix", "AI fix remaining errors", s_ai_fix),
    ("verify", "Verify", s_verify),
]

PATCH_STEPS = [
    ("patchcheck", "Patch preflight", s_patch_check),
    ("patchhandler", "Update app/Exceptions/Handler.php", s_patch_handler),
    ("patchcontroller", "Update app/Http/Controllers/Controller.php", s_patch_controller),
    ("patchroutes", "Upgrade route files for Laravel 12", s_patch_routes),
    ("patchverify", "Verify", s_patch_verify),
    ("patchmark", "Mark site as patched", s_patch_mark),
]


REPAIR = {"preflight", "bootstrap", "auth", "env", "session", "home", "web", "perms", "aifix", "verify"}


def plan(opts):
    if opts.get("patch"):
        return list(PATCH_STEPS)
    if opts.get("dry_run"):
        return STEPS[:1]
    steps = [s for s in STEPS if s[0] in REPAIR] if opts.get("repair") else list(STEPS)
    if not opts.get("ai"):
        steps = [s for s in steps if s[0] != "aifix"]
    return steps


def log_error(job, step, err, tb):
    with open(DATA / "errors.jsonl", "a") as f:
        f.write(json.dumps({"time": time.strftime("%F %T"), "job": job.id, "project": job.project, "step": step, "error": err, "trace": tb}) + "\n")


def free_path(p):
    if not p.exists() and not p.is_symlink():
        return p
    i = 1
    while True:
        q = p.with_name("%s.%d" % (p.name, i))
        if not q.exists() and not q.is_symlink():
            return q
        i += 1


def check_restore(job):
    bad = []
    for a in job.manifest:
        try:
            if a["t"] == "rename":
                if not (job.path / a["from"]).exists():
                    bad.append("missing after rollback: " + a["from"])
            elif a["t"] == "modify":
                p, b = job.path / a["path"], job.path / a["backup"]
                if p.is_file() and b.is_file() and p.read_bytes() != b.read_bytes():
                    bad.append("not restored: " + a["path"])
        except Exception as ex:
            bad.append("%s: %s" % (a.get("path") or a.get("from"), ex))
    return bad


def restore(job):
    job.log("--- rollback")
    for a in reversed(job.manifest):
        try:
            if a["t"] == "modify":
                shutil.copy2(job.path / a["backup"], job.path / a["path"])
            elif a["t"] == "create":
                p = job.path / a["path"]
                if p.exists():
                    p.rename(free_path(p.with_name(p.name + ".rolled-" + job.id)))
            elif a["t"] == "rename":
                cur, old = job.path / a["from"], job.path / a["to"]
                if cur.exists():
                    cur.rename(free_path(cur.with_name(cur.name + "_failed_" + job.id)))
                if old.exists():
                    old.rename(cur)
            job.log("undone " + json.dumps(a))
        except Exception as ex:
            job.log("rollback error %s: %s" % (a, ex))
    job.rolled_back = True
    bad = check_restore(job)
    if bad:
        job.warn("rollback check found problems, nothing was deleted so the files can be fixed by hand: " + "; ".join(bad[:6]))
    else:
        job.log("rollback verified: %d changes undone, no file was deleted" % len(job.manifest))


def short_text(t, n=140):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def fmt_secs(n):
    n = int(n)
    return "%ds" % n if n < 60 else "%dm %02ds" % (n // 60, n % 60)


def plural(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


def job_log_text(job):
    f = JOBDIR / (job.id + ".log")
    return f.read_text(errors="replace") if f.is_file() else ""


def home_status(job):
    m = re.search(r"PROBE (\S+) -> (\d{3})", job_log_text(job))
    return int(m.group(2)) if m else None


def plain_reason(step, err):
    low = (err or "").lower()
    if "health gate" in low:
        return "the upgraded site did not pass the final health check"
    if "below 8.2" in low:
        return "the PHP version on the server is too old for Laravel 12"
    if "not a laravel project" in low:
        return "the folder is not a Laravel project"
    if "free disk" in low:
        return "there is not enough free disk space"
    if "not writable by" in low:
        return "the folder is owned by another user and this tool cannot change it"
    if "already on laravel" in low:
        return "the site is already on Laravel 12"
    if "not on laravel 12 yet" in low:
        return "the site is not on Laravel 12 yet"
    if step in ("resolve", "composer", "composerjson", "skelprep") or "composer" in low:
        return "the required packages could not be prepared or installed"
    return short_text(err, 120) or "an unexpected error happened"


def build_conclusion(job):
    st = job.status
    if st in ("running", "queued"):
        return None
    steps = job.steps or []
    total = len(steps)
    done = len([s for s in steps if s.get("status") == "done"])
    secs = sum(float(s.get("secs") or 0) for s in steps)
    warns = list(job.warnings or [])
    home = home_status(job)
    facts = [["Steps", "%d / %d" % (done, total)], ["Time", fmt_secs(secs)],
             ["Home page", str(home) if home else "—"], ["Warnings", str(len(warns))]]
    ver = (job.state or {}).get("from_version") or ""
    if st == "rolled_back":
        return {"verdict": "info", "title": "Run was rolled back", "facts": facts, "issues": [],
                "text": "This run was rolled back, so every change it made was undone. "
                        "The site is back to how it was before the migration. "
                        "Nothing from this run is active now, and you can start a new migration whenever you are ready."}
    if st == "failed":
        bad = next((s for s in steps if s.get("status") == "error"), None)
        err = (bad or {}).get("error") or ""
        title = (bad or {}).get("title") or "a step"
        reason = plain_reason((bad or {}).get("key"), err)
        text = 'The migration stopped at the step "%s" because %s. ' % (title, reason)
        if job.rolled_back:
            text += "All changes were rolled back automatically, so the site stays in its old state and the upgrade was not applied. "
        elif not job.manifest:
            text += "No file was changed, so the site is untouched. "
        else:
            text += "The changes were not rolled back, so the site may not load until this is fixed. "
        text += "Fix the cause and start the migration again."
        return {"verdict": "bad", "title": "Migration failed, site was not upgraded" if (job.rolled_back or not job.manifest) else "Migration failed, site may be broken",
                "facts": facts, "issues": [short_text(err, 220)] if err else [], "text": text}
    if st == "interrupted":
        return {"verdict": "bad", "title": "Run stopped unexpectedly", "facts": facts, "issues": [],
                "text": "The run stopped before it finished, for example because the server restarted. "
                        "The site may be left half changed and may not load. "
                        "Use Rollback this run to undo the changes, then start the migration again."}
    if (job.opts or {}).get("patch"):
        files = (job.state or {}).get("patch_files") or []
        s1 = "The Laravel 12 patch updated %s and the site is marked as patched." % (
            ", ".join(files) if files else "nothing, every file was already up to date")
        if warns or not home:
            return {"verdict": "warn", "title": "Site patched, with issues to review", "facts": facts,
                    "issues": [short_text(w, 170) for w in warns[:4]],
                    "text": s1 + " The items listed below need a look, and the home page result should be confirmed in a browser."}
        return {"verdict": "ok", "title": "Site patched successfully", "facts": facts, "issues": [],
                "text": s1 + " The site starts and the home page answered with status %s." % home}
    if (job.opts or {}).get("dry_run"):
        return {"verdict": "info", "title": "Preflight passed, nothing changed", "facts": facts, "issues": [],
                "text": "Only the preflight checks were run, and they passed. "
                        "The folder is a Laravel project%s and the server has a suitable PHP version and enough disk space. "
                        "No file was changed, so the site is untouched. Start the full migration when you are ready."
                        % (" (Laravel %s)" % ver if ver else "")}
    repair = bool((job.opts or {}).get("repair"))
    if repair:
        s1 = "Repair mode re-applied the Laravel 12 fixes on a site that was already upgraded."
    else:
        s1 = "%s was upgraded to Laravel 12 and the new framework files were installed." % ("Laravel " + ver if ver else "The site")
    dropped = (job.state or {}).get("dropped") or []
    ai = [n for n in (job.notes or []) if str(n).startswith("AI edited")]
    parts = []
    if dropped:
        parts.append("%s with no Laravel 12 support %s removed from composer (%s), the old copies are kept" % (
            plural(len(dropped), "package", "packages"), "was" if len(dropped) == 1 else "were",
            ", ".join(dropped[:3]) + (", ..." if len(dropped) > 3 else "")))
    if (job.state or {}).get("citigate"):
        parts.append("the citigate gateway was kept in place")
    if ai:
        parts.append("the AI fixed %s" % plural(len(ai), "file", "files"))
    s2 = ("Along the way, " + " and ".join(parts) + ". ") if parts else ""
    env_err = any("database or environment error" in w for w in warns)
    issues = [short_text(w, 170) for w in warns[:4]]
    if len(warns) > 4:
        issues.append("and %d more warnings, see the list above" % (len(warns) - 4))
    if env_err:
        verdict = "warn"
        title = "Upgraded, but the database or .env needs a fix"
        s3 = "The framework starts, but the home page returned an error because the database or .env settings are not correct."
        s4 = "Fix the database details in .env and open the site again, the upgrade itself does not need to be repeated."
    elif home is None:
        verdict = "warn"
        title = "Upgraded, but not fully confirmed"
        s3 = "The final home page check gave no result, so it is not certain that the site opens."
        s4 = "Open the site in a browser to confirm it works."
    elif not warns:
        verdict = "ok"
        title = "Site is running successfully"
        s3 = "The site starts and the home page answered with status %s, and no warnings were found." % home
        s4 = "Payments, admin, uploads and scheduled tasks are not tested by this tool, so click through them once."
    else:
        verdict = "warn"
        title = "Site is running, with issues to review"
        s3 = "The site starts and the home page answered with status %s, but %s need attention." % (home, plural(len(warns), "warning", "warnings"))
        s4 = "The items listed below are the parts that may not work yet, so fix them first."
    return {"verdict": verdict, "title": title, "facts": facts, "issues": issues,
            "text": " ".join(x for x in (s1, s2.strip(), s3, s4) if x)}


def run(job):
    job.pid = os.getpid()
    job.status = "running"
    job.save()
    steps = plan(job.opts)
    try:
        for s, (k, t, fn) in zip(job.steps, steps):
            s["status"] = "running"
            job.save()
            t0 = time.time()
            job.log("--- " + t)
            try:
                fn(job)
                s["status"] = "done"
            except Exception as e:
                s["status"] = "error"
                s["error"] = str(e)
                job.log("ERROR " + str(e))
                log_error(job, k, str(e), traceback.format_exc())
                job.status = "failed"
                if job.manifest and job.opts.get("auto_rollback", True):
                    restore(job)
                    job.log("all changes of this job were rolled back automatically")
                return
            finally:
                s["secs"] = round(time.time() - t0, 1)
                job.save()
        job.status = "done" if not job.warnings else "done_with_warnings"
    finally:
        sc = job.state.get("scratch")
        if sc and Path(sc).parent == WORK:
            shutil.rmtree(sc, ignore_errors=True)
        try:
            job.conclusion = build_conclusion(job)
        except Exception:
            job.conclusion = None
        job.save()


def alive(j):
    if not j.pid:
        return time.time() - j.created < 60
    try:
        os.kill(j.pid, 0)
        return True
    except OSError:
        return False


def load(jid, build=True):
    f = JOBDIR / (re.sub(r"[^\w\-]", "", jid) + ".json")
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    j = Job(d["project"], d["opts"], d)
    if j.status in ("running", "queued") and not alive(j):
        j.status = "interrupted"
        j.save()
    if build and not j.conclusion and j.status not in ("running", "queued"):
        try:
            j.conclusion = build_conclusion(j)
            j.save()
        except Exception:
            pass
    return j


def all_jobs():
    out = []
    for f in sorted(JOBDIR.glob("*.json")):
        try:
            out.append(load(f.stem, False))
        except Exception:
            pass
    return [j for j in out if j]


def start(project, opts):
    project = project.strip()
    if not project:
        raise ValueError("enter a project folder name or absolute path")
    p = resolve(project)
    if not is_laravel(p):
        raise ValueError("no Laravel project (composer.json requiring laravel/framework) at %s" % p)
    for j in all_jobs():
        if j.path == p and j.status in ("running", "queued"):
            raise ValueError("a job is already running for this project")
    o = dict(opts)
    o["ai"] = bool(o.get("ai", True)) and bool(api_key())
    o["repair"] = installed_major(p) >= 12
    if o.get("patch"):
        if not o["repair"]:
            raise ValueError("the site is not on Laravel 12 yet, run the migration first")
        o["ai"] = False
        o["dry_run"] = False
        o["repair"] = False
    extra = [x.strip().strip("/") for x in (o.get("extra_paths") or "").split(",") if x.strip()]
    o["probe_paths"] = ["/"] + ["/" + x for x in extra]
    base = (o.get("base_url") or "").strip().rstrip("/")
    if base:
        u = urlparse(base)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("base URL must look like https://host or https://host/folder")
        o["app_url"] = base
        if u.path:
            o["staging_host"] = u.netloc
            o["staging_base"] = u.path
        urls = [base + "/"] + [base + "/" + x for x in extra]
        o["check_urls"] = ",".join(urls)
    job = Job(project, o)
    job.save()
    subprocess.Popen([sys.executable, str(BASE / "worker.py"), job.id], cwd=str(BASE), start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(DATA / "worker.err", "a"))
    return job.view()


def rollback(jid):
    job = load(jid)
    if not job:
        raise ValueError("unknown job")
    if job.status in ("running", "queued"):
        raise ValueError("job still running")
    if job.rolled_back:
        raise ValueError("already rolled back")
    restore(job)
    if (job.opts or {}).get("patch"):
        unmark_patched(job)
    job.status = "rolled_back"
    try:
        job.conclusion = build_conclusion(job)
    except Exception:
        pass
    job.save()
    return job.view()


def added_time(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def projects():
    jobs = all_jobs()
    marks = patched_map()
    out = []
    for top in sorted(ROOT.iterdir(), key=added_time, reverse=True):
        try:
            if not top.is_dir():
                continue
            if is_laravel(top):
                d, name = top, top.name
            elif is_laravel(top / "core"):
                d, name = top / "core", top.name + "/core"
            else:
                continue
        except OSError:
            continue
        try:
            req = json.loads((d / "composer.json").read_text()).get("require", {})
        except Exception:
            req = {}
        try:
            fw = laravel_version(d) or "?"
            migrated = installed_major(d) >= 12
        except OSError:
            continue
        last = sorted([j for j in jobs if j.path == d.resolve()], key=lambda j: j.id)
        out.append({"name": name, "laravel": fw, "php": req.get("php", "?"),
                    "migrated": migrated, "patched": name in marks, "last": last[-1].status if last else ""})
    return out