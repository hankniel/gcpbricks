"""List prices from the Cloud Billing Catalog API, for the console's cost estimates. The catalog
is public to any signed-in principal (no billing account role): what a SKU costs before
discounts, credits and committed-use, per region.

A resource's price is a sum of SKUs, matched on the catalog's descriptions, which carry the
machine family, GPU and billing kind but no machine shape:

    Colab runtime      Vertex Colab {CPU, RAM, GPU} for GCE usage + management fee, disk per GiB-month
    Vertex training    Vertex AI: Training/Pipelines on {family} core / RAM / GPU + management fee
    Vertex endpoint    Vertex AI: Online/Batch Prediction {family} core / RAM / GPU + management fee
    Cloud Run          Jobs CPU / Jobs Memory; Services CPU and Memory, request- or instance-based
    BigQuery           Analysis (on-demand, per TiB billed)

A machine's cores and memory come from Compute Engine's machine types. Each price says which
SKUs made it, and a resource no SKU matches has no price rather than a guessed one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SERVICES = {"vertex": "C7E2-9256-1C43", "run": "152E-C115-5142", "bigquery": "24E6-581D-38E5"}
#: The SKUs this module matches, as the catalog listed them on a day: what the console prices
#: with when its project hasn't enabled the Cloud Billing API (cloudbilling.googleapis.com).
SNAPSHOT = Path(__file__).with_name("price_snapshot.json")
#: Accelerator types as the catalog names their GPU (Colab: "T4"; Vertex: "Nvidia Tesla T4").
GPUS = {"NVIDIA_TESLA_T4": "T4", "NVIDIA_L4": "L4", "NVIDIA_TESLA_V100": "V100", "NVIDIA_TESLA_P100": "P100",
        "NVIDIA_TESLA_P4": "P4", "NVIDIA_TESLA_A100": "A100", "NVIDIA_A100_80GB": "A100 80GB",
        "NVIDIA_H100_80GB": "H100"}
DISKS = {"pd-standard": "Standard", "pd-balanced": "Balanced", "pd-ssd": "SSD"}
HOURS_PER_MONTH = 730


def _unit_price(sku: dict) -> float:
    expr = sku["pricingInfo"][0]["pricingExpression"]
    rate = expr["tieredRates"][-1]["unitPrice"]  # the list rate past any free tier
    return int(rate.get("units") or 0) + rate.get("nanos", 0) / 1e9


def _clean(text: str) -> str:
    # One Colab SKU spells CPU with a Cyrillic С; another starts with a space.
    return text.replace("С", "C").strip()


def catalog(gcp) -> tuple[object, str]:
    """(service id -> SKUs, where the prices come from): the live catalog, else the snapshot."""
    try:
        gcp.catalog_skus(SERVICES["bigquery"])
        return gcp.catalog_skus, "the Cloud Billing Catalog, today"
    except Exception as e:  # noqa: BLE001 - the API disabled in the project, or not reachable
        snap = json.loads(SNAPSHOT.read_text())
        why = "the Cloud Billing API isn't enabled in the console's project" if "has not been used" in getattr(
            getattr(e, "response", None), "text", "") else f"the catalog didn't answer ({type(e).__name__})"
        return (lambda sid: snap["skus"].get(sid, [])), f"the catalog as of {snap['as_of']} ({why})"


class Prices:
    """Per-unit list prices in one region; `skus(service)` the catalog's SKUs of a service."""

    def __init__(self, skus_of, region: str):
        self._skus_of, self.region = skus_of, region
        self._by: dict[str, list[dict]] = {}

    def skus(self, service: str) -> list[dict]:
        if service not in self._by:
            self._by[service] = [s for s in self._skus_of(SERVICES[service])
                                 if s.get("category", {}).get("usageType") == "OnDemand"]
        return self._by[service]

    def match(self, service: str, pattern: str, regions: tuple[str, ...] | None = None) -> dict | None:
        """The one SKU of `service` in this region (or `regions`) whose description matches
        `pattern`; the catalog's "for AI Platform" twins are the legacy product's, not Vertex's."""
        want = regions or (self.region,)
        rx = re.compile(pattern, re.IGNORECASE)
        hits = [s for s in self.skus(service) if rx.search(_clean(s["description"]))
                and any(r in s.get("serviceRegions", []) for r in want)]
        hits = [s for s in hits if "for AI Platform" not in s["description"]] or hits
        if not hits:
            return None
        s = min(hits, key=lambda s: len(s["description"]))  # the plainest, when two are left
        return {"sku": s["skuId"], "description": s["description"], "usd": _unit_price(s),
                "unit": s["pricingInfo"][0]["pricingExpression"]["usageUnit"]}

    def _sum(self, parts: list[tuple[float, dict | None]]) -> dict:
        """sum(quantity * SKU price); missing names the SKUs the catalog didn't have."""
        missing = [p for q, p in parts if q and p is None]
        return {"usd_per_hour": None if missing else sum(q * p["usd"] for q, p in parts if q and p),
                "skus": [p for _, p in parts if p], "missing": len(missing)}

    # --- machines by the hour ------------------------------------------------------------

    def colab_hour(self, machine: dict, gpu: str | None, gpus: int) -> dict:
        fam = machine["family"]

        def both(kind: str, what: str) -> list[dict | None]:
            return [self.match("vertex", rf"^Vertex Colab {kind} for GCE usage - {what}"),
                    self.match("vertex", rf"^Vertex Colab {kind} for GCE management fee - {what}")]
        cpu, ram = both("CPU", rf"{fam} Instance Cores\b"), both("RAM", rf"{fam} Instance Ram\b")
        g = both("GPU", rf"{re.escape(gpu)}( |$)") if gpu else []
        return self._sum([(machine["cpus"], p) for p in cpu] + [(machine["memory_gib"], p) for p in ram]
                         + [(gpus, p) for p in g])

    def colab_disk_month(self, disk_type: str, gib: float) -> dict:
        kind = DISKS.get(disk_type, "Standard")
        parts = [self.match("vertex", rf"^Vertex Colab Storage for GCE usage - {kind} Persistent Disk"),
                 self.match("vertex", rf"^Vertex Colab Storage for GCE management fee - {kind} Persistent Disk")]
        got = self._sum([(gib, p) for p in parts])
        return {**got, "usd_per_month": got.pop("usd_per_hour")}

    def vertex_hour(self, use: str, machine: dict, gpu: str | None, gpus: int) -> dict:
        """use: "training" (custom jobs, pipeline steps) or "prediction" (endpoints)."""
        head = {"training": "Vertex AI: Training/Pipelines", "prediction": "Vertex AI: Online/Batch Prediction"}[use]
        fam = machine["family"]
        on = "on " if use == "training" else ""
        parts = [(machine["cpus"], self.match("vertex", rf"^{head} {on}{fam} (Predefined )?Instance Core running")),
                 (machine["cpus"], self.match("vertex", rf"^{head} Management fee on {fam} (Predefined )?Instance Core")),
                 (machine["memory_gib"], self.match("vertex", rf"^{head} {on}{fam} (Predefined )?Instance Ram running")),
                 (machine["memory_gib"], self.match("vertex", rf"^{head} Management fee on {fam} (Predefined )?Instance RAM"))]
        if gpu:
            name = rf"(Tesla )?{re.escape(gpu)}"
            parts += [(gpus, self.match("vertex", rf"^{head} {on}Nvidia {name} GPU running")),
                      (gpus, self.match("vertex", rf"^{head} Management fee on NVIDIA {name}\b"))]
        return self._sum(parts)

    # --- Cloud Run and BigQuery ----------------------------------------------------------

    def cloud_run(self, kind: str) -> dict:
        """Per vCPU-second and GiB-second. kind: "job", "service" (request-based: CPU only
        while serving), "service_min" (a request-based service's idle minimum instances) or
        "service_instance" (instance-based: CPU always allocated)."""
        cpu, mem = {"job": (r"^Jobs CPU\b", r"^Jobs Memory\b"),
                    "service": (r"^Services CPU \(Request-based billing\)", r"^Services Memory \(Request-based billing\)"),
                    "service_min": (r"^Services Min Instance CPU \(Request-based billing\)",
                                    r"^Services Min Instance Memory \(Request-based billing\)"),
                    "service_instance": (r"^Services CPU \(Instance-based billing\)", r"^Instances Memory\b")}[kind]
        c, m = self.match("run", cpu), self.match("run", mem)
        return {"usd_per_vcpu_s": c and c["usd"], "usd_per_gib_s": m and m["usd"], "skus": [p for p in (c, m) if p]}

    def bigquery_tib(self, location: str = "us") -> dict | None:
        return self.match("bigquery", r"^Analysis$", (location.lower(),))


def machine_shape(m: dict, machine_type: str) -> dict:
    """{family, cpus, memory_gib} of a Compute Engine machine type resource (e2-standard-4: E2, 4, 16)."""
    return {"family": machine_type.split("-")[0].upper(), "cpus": m["guestCpus"], "memory_gib": m["memoryMb"] / 1024}


def gpu_name(accelerator_type: str | None) -> str | None:
    if not accelerator_type or accelerator_type == "ACCELERATOR_TYPE_UNSPECIFIED":
        return None
    return GPUS.get(accelerator_type, accelerator_type.removeprefix("NVIDIA_").removeprefix("TESLA_").replace("_", " "))
