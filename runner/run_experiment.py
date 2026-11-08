"""Раннер: план из YAML -> для каждого прогона пересоздать SUT, прогрев, замер, манифест.

Использование: python runner/run_experiment.py configs/experiment.yaml
Возобновляемый: прогоны с готовым манифестом пропускаются.
"""
import hashlib, itertools, json, os, platform, random, shutil, subprocess, sys, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
cfg_path = Path(sys.argv[1] if len(sys.argv) > 1 else "configs/experiment.yaml")
cfg = yaml.safe_load(cfg_path.read_text())
OUT = ROOT / cfg["out_dir"]
os.environ["RESULTS_DIR"] = cfg["out_dir"]


def _compose_cmd():
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


DC = _compose_cmd()


def sh(*args, env=None, check=True):
    r = subprocess.run(list(args), capture_output=True, text=True, env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        print(f"КОМАНДА УПАЛА ({r.returncode}): {' '.join(args)}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}", flush=True)
        r.check_returncode()
    return r.stdout.strip()


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def images_info():
    out = {}
    for svc, img in {"sut": "asyncstand-sut:1", "db": "postgres:16.4",
                     "load": "locustio/locust:2.31.5"}.items():
        out[svc] = {"tag": img, "id": sh("docker", "image", "inspect", img, "--format", "{{.Id}}", check=False)}
    return out


def host_info():
    d = json.loads(sh("docker", "info", "--format", "{{json .}}"))
    return {"os": platform.platform(), "machine": platform.machine(), "host_cpu_count": os.cpu_count(),
            "docker_server": d.get("ServerVersion"), "docker_vm_cpus": d.get("NCPU"),
            "docker_vm_mem_bytes": d.get("MemTotal"), "docker_vm_kernel": d.get("KernelVersion"),
            "python": platform.python_version()}


def build_plan():
    f = cfg["factors"]
    runs = [dict(zip(f, c), label="main", rep=r)
            for c in itertools.product(*f.values()) for r in range(cfg["repetitions"])]
    if "aa" in cfg:
        aa = {k: v for k, v in cfg["aa"].items() if k != "repetitions"}
        runs += [dict(aa, label="aa", rep=r) for r in range(cfg["aa"]["repetitions"])]
    if cfg.get("randomize_order", True):
        random.Random(cfg["seed"]).shuffle(runs)
    return runs


def sampler(stop, path):
    """CPU/RAM SUT, БД и генератора через docker stats (проценты от одного ядра)."""
    names = ["asyncstand-sut", "asyncstand-load"]
    with open(path, "w") as f:
        f.write("ts,container,cpu_pct,mem_mib\n")
        while not stop.is_set():
            try:
                o = sh("docker", "stats", "--no-stream", "--format",
                       "{{.Name}},{{.CPUPerc}},{{.MemUsage}}", *names)
                for line in o.splitlines():
                    n, cpu, mem = line.split(",")
                    mem = mem.split("/")[0].strip()
                    mib = float(mem[:-3]) * (1024 if mem.endswith("GiB") else 1) if mem.endswith("iB") else 0
                    f.write(f"{time.time():.1f},{n},{cpu.strip('%')},{mib}\n")
                f.flush()
            except Exception:
                time.sleep(0.5)


def locust(env, users, seconds, prefix):
    sh(*DC, "--profile", "run", "run", "--rm", "--name", "asyncstand-load",
       "-e", f"WORKLOAD={env['WORKLOAD']}", "-e", f"SEED={env['SEED']}", "load",
       "-f", "/mnt/locust/locustfile.py", "--headless", "--processes", "2",
       "-u", str(users), "-r", str(users), "-t", f"{seconds}s",
       "--host", "http://sut:8000", "--csv", f"/results/{prefix}", "--only-summary")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.chmod(0o777)  # контейнер Locust работает от другого uid (на Linux-раннерах CI иначе нет прав записи)
    plan = build_plan()
    base = {"config_file": str(cfg_path), "config_sha256": sha256(cfg_path),
            "uv_lock_sha256": sha256("sut/uv.lock"), "seed": cfg["seed"],
            "git_commit": sh("git", "rev-parse", "HEAD", check=False) or "no-commits",
            "git_dirty": bool(sh("git", "status", "--porcelain", "--", ".", ":!results", check=False)),
            "images": images_info(), "host": host_info()}
    (OUT / "plan.json").write_text(json.dumps({"config": cfg, "env": base, "plan": plan}, indent=1))
    sh(*DC, "up", "-d", "--wait", "db")
    if cfg.get("monitoring"):
        sh(*DC, "--profile", "monitoring", "up", "-d", "prometheus", "cadvisor", "grafana")
    for i, p in enumerate(plan, 1):
        run_id = f"{i:04d}_{p['label']}_{p['driver']}_{p['workload']}_u{p['users']}_r{p['rep']}"
        if (OUT / f"{run_id}.json").exists():
            continue
        print(f"[{i}/{len(plan)}] {run_id}", flush=True)
        env = {"DRIVER": p["driver"], "POOL_SIZE": str(cfg["pool_size"])}
        sh(*DC, "up", "-d", "--force-recreate", "--no-deps", "--wait", "sut", env=env)
        renv = {"WORKLOAD": p["workload"], "SEED": cfg["seed"] + i}
        locust(renv, p["users"], cfg["warmup_seconds"], f"warmup_{run_id}")
        stop = threading.Event()
        th = threading.Thread(target=sampler, args=(stop, OUT / f"{run_id}_docker_stats.csv"))
        th.start()
        started = datetime.now(timezone.utc).isoformat()
        try:
            locust(renv, p["users"], cfg["duration_seconds"], run_id)
        finally:
            stop.set(); th.join()
        man = {**base, "run_id": run_id, "order": i, "params": {k: p[k] for k in ("driver", "workload", "users")},
               "label": p["label"], "repetition": p["rep"], "run_seed": renv["SEED"],
               "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat()}
        (OUT / f"{run_id}.json").write_text(json.dumps(man, indent=1))
    sh(*DC, "--profile", "monitoring", "down", "-v", check=False)


if __name__ == "__main__":
    main()
