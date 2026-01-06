#!/usr/bin/env python3
"""
Prometheus Metrics Snapshot Tool
Captures and compares Prometheus metrics across platform releases.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
SNAPSHOTS_DIR = SCRIPT_DIR / "snapshots"
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
DEFAULT_PROM_NAMESPACE = "glueops-core-kube-prometheus-stack"
DEFAULT_PROM_SERVICE = "prometheus-operated"
DEFAULT_PROM_PORT = 9090


def load_config():
    """Load configuration from config.yaml if exists."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f)
    return {}


def get_platform_version(cluster_path: str = None) -> dict:
    """Read platform version from VERSIONS/glueops.yaml."""
    version_info = {
        "platform_version": "unknown",
        "argocd_version": "unknown",
        "codespace_version": "unknown"
    }
    
    # Try to find VERSIONS directory
    search_paths = []
    if cluster_path:
        search_paths.append(Path(cluster_path) / "VERSIONS" / "glueops.yaml")
    
    # Also check common locations
    search_paths.extend([
        Path("/workspaces/glueops") / os.environ.get("CLUSTER", "") / "VERSIONS" / "glueops.yaml",
        Path.cwd() / "VERSIONS" / "glueops.yaml",
    ])
    
    # Auto-detect cluster directories
    workspace_root = Path("/workspaces/glueops")
    if workspace_root.exists():
        for versions_file in workspace_root.glob("*/VERSIONS/glueops.yaml"):
            search_paths.append(versions_file)

    for version_file in search_paths:
        if version_file.exists():
            try:
                with open(version_file) as f:
                    data = yaml.safe_load(f)
                    # Handle versions array format
                    if "versions" in data:
                        for item in data["versions"]:
                            name = item.get("name", "")
                            version = item.get("version", "unknown")
                            if name == "glueops_platform_helm_chart_version":
                                version_info["platform_version"] = version
                            elif name == "argocd_app_version":
                                version_info["argocd_version"] = version
                            elif name == "codespace_version":
                                version_info["codespace_version"] = version
                    else:
                        # Fallback to flat format
                        version_info["platform_version"] = data.get("glueops_platform_helm_chart_version", "unknown")
                        version_info["argocd_version"] = data.get("argocd_app_version", "unknown")
                        version_info["codespace_version"] = data.get("codespace_version", "unknown")
                    break
            except Exception as e:
                print(f"Warning: Could not read {version_file}: {e}", file=sys.stderr)
    
    return version_info


def get_captain_domain() -> str:
    """Get captain domain from environment or saved_variables."""
    # Check environment
    if "CLUSTER" in os.environ:
        return os.environ["CLUSTER"]
    
    # Check saved_variables
    saved_vars = Path("/workspaces/glueops/saved_variables")
    if saved_vars.exists():
        with open(saved_vars) as f:
            for line in f:
                if line.startswith("CLUSTER="):
                    return line.strip().split("=", 1)[1]
    
    return "unknown"


def start_port_forward(namespace: str, service: str, local_port: int, remote_port: int) -> subprocess.Popen:
    """Start kubectl port-forward in background."""
    cmd = [
        "kubectl", "-n", namespace,
        "port-forward", f"svc/{service}",
        f"{local_port}:{remote_port}"
    ]
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    # Wait for port-forward to be ready
    time.sleep(25)
    
    if proc.poll() is not None:
        raise RuntimeError(f"Failed to start port-forward to {namespace}/{service}")
    
    return proc


def fetch_metrics(prometheus_url: str) -> list:
    """Fetch all metric names from Prometheus."""
    url = f"{prometheus_url}/api/v1/label/__name__/values"
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus API error: {data}")
        
        return sorted(data.get("data", []))
    
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch metrics: {e}")


def save_snapshot(metrics: list, output_file: Path, metadata: dict):
    """Save metrics snapshot to YAML file."""
    snapshot = {
        "metadata": {
            **metadata,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "metrics_count": len(metrics)
        },
        "metrics": metrics
    }
    
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, "w") as f:
        yaml.dump(snapshot, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    
    return snapshot


def load_snapshot(snapshot_file: Path) -> dict:
    """Load snapshot from YAML file."""
    with open(snapshot_file) as f:
        return yaml.safe_load(f)


def compare_snapshots(snapshot_a: dict, snapshot_b: dict) -> dict:
    """Compare two snapshots and return differences."""
    metrics_a = set(snapshot_a.get("metrics", []))
    metrics_b = set(snapshot_b.get("metrics", []))
    
    common = sorted(metrics_a & metrics_b)
    only_in_a = sorted(metrics_a - metrics_b)
    only_in_b = sorted(metrics_b - metrics_a)
    
    return {
        "comparison": {
            "snapshot_a": {
                "file": snapshot_a.get("_source_file", "unknown"),
                "version": snapshot_a.get("metadata", {}).get("platform_version", "unknown"),
                "timestamp": snapshot_a.get("metadata", {}).get("timestamp", "unknown"),
                "total_metrics": len(metrics_a)
            },
            "snapshot_b": {
                "file": snapshot_b.get("_source_file", "unknown"),
                "version": snapshot_b.get("metadata", {}).get("platform_version", "unknown"),
                "timestamp": snapshot_b.get("metadata", {}).get("timestamp", "unknown"),
                "total_metrics": len(metrics_b)
            },
            "summary": {
                "common_metrics": len(common),
                "unique_to_a": len(only_in_a),
                "unique_to_b": len(only_in_b)
            }
        },
        "common_metrics": common,
        "unique_to_snapshot_a": only_in_a,
        "unique_to_snapshot_b": only_in_b
    }


def cmd_snapshot(args):
    """Take a snapshot of current Prometheus metrics."""
    config = load_config()
    
    namespace = args.namespace or config.get("prometheus_namespace", DEFAULT_PROM_NAMESPACE)
    service = args.service or config.get("prometheus_service", DEFAULT_PROM_SERVICE)
    port = args.port or config.get("prometheus_port", DEFAULT_PROM_PORT)
    
    port_forward_proc = None
    
    try:
        # Start port-forward if not using external URL
        if args.url:
            prometheus_url = args.url.rstrip("/")
        else:
            print(f"Starting port-forward to {namespace}/{service}:{port}...", file=sys.stderr)
            port_forward_proc = start_port_forward(namespace, service, port, port)
            prometheus_url = f"http://localhost:{port}"
        
        print(f"Fetching metrics from {prometheus_url}...", file=sys.stderr)
        metrics = fetch_metrics(prometheus_url)
        print(f"Found {len(metrics)} metrics", file=sys.stderr)
        
        # Gather metadata
        version_info = get_platform_version(args.cluster_path)
        metadata = {
            "platform_version": version_info["platform_version"],
            "argocd_version": version_info["argocd_version"],
            "codespace_version": version_info["codespace_version"],
            "captain_domain": get_captain_domain(),
            "prometheus_namespace": namespace
        }
        
        # Generate output filename
        if args.output:
            output_file = Path(args.output)
        else:
            version_slug = version_info["platform_version"].replace(".", "-")
            timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
            output_file = SNAPSHOTS_DIR / f"{version_slug}_{timestamp}.yaml"
        
        # Save snapshot
        snapshot = save_snapshot(metrics, output_file, metadata)
        
        print(f"\n✅ Snapshot saved: {output_file}", file=sys.stderr)
        print(f"   Platform version: {metadata['platform_version']}", file=sys.stderr)
        print(f"   Metrics count: {len(metrics)}", file=sys.stderr)
        
        if args.json:
            print(json.dumps(snapshot, indent=2))
        
        return 0
    
    finally:
        if port_forward_proc:
            port_forward_proc.terminate()
            port_forward_proc.wait()


def cmd_compare(args):
    """Compare two snapshots."""
    snapshot_a_path = Path(args.snapshot_a)
    snapshot_b_path = Path(args.snapshot_b)
    
    # Handle "latest" keyword
    if args.snapshot_a == "latest" or args.snapshot_b == "latest":
        snapshots = sorted(SNAPSHOTS_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not snapshots:
            print("Error: No snapshots found", file=sys.stderr)
            return 1
        
        if args.snapshot_a == "latest":
            snapshot_a_path = snapshots[0]
        if args.snapshot_b == "latest":
            snapshot_b_path = snapshots[0]
    
    if not snapshot_a_path.exists():
        print(f"Error: Snapshot not found: {snapshot_a_path}", file=sys.stderr)
        return 1
    
    if not snapshot_b_path.exists():
        print(f"Error: Snapshot not found: {snapshot_b_path}", file=sys.stderr)
        return 1
    
    snapshot_a = load_snapshot(snapshot_a_path)
    snapshot_a["_source_file"] = str(snapshot_a_path.name)
    
    snapshot_b = load_snapshot(snapshot_b_path)
    snapshot_b["_source_file"] = str(snapshot_b_path.name)
    
    result = compare_snapshots(snapshot_a, snapshot_b)
    
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_comparison_report(result, args.verbose)
    
    return 0


def print_comparison_report(result: dict, verbose: bool = False):
    """Print human-readable comparison report."""
    comp = result["comparison"]
    
    print("\n" + "=" * 60)
    print("PROMETHEUS METRICS COMPARISON REPORT")
    print("=" * 60)
    
    print(f"\n📊 Snapshot A: {comp['snapshot_a']['file']}")
    print(f"   Version: {comp['snapshot_a']['version']}")
    print(f"   Date: {comp['snapshot_a']['timestamp']}")
    print(f"   Total metrics: {comp['snapshot_a']['total_metrics']}")
    
    print(f"\n📊 Snapshot B: {comp['snapshot_b']['file']}")
    print(f"   Version: {comp['snapshot_b']['version']}")
    print(f"   Date: {comp['snapshot_b']['timestamp']}")
    print(f"   Total metrics: {comp['snapshot_b']['total_metrics']}")
    
    print("\n" + "-" * 60)
    print("SUMMARY")
    print("-" * 60)
    
    summary = comp["summary"]
    print(f"✅ Common metrics:        {summary['common_metrics']}")
    print(f"🔵 Unique to Snapshot A:  {summary['unique_to_a']}")
    print(f"🟢 Unique to Snapshot B:  {summary['unique_to_b']}")
    
    if verbose or summary["unique_to_a"] <= 50:
        if result["unique_to_snapshot_a"]:
            print("\n" + "-" * 60)
            print("🔵 METRICS UNIQUE TO SNAPSHOT A (missing in B)")
            print("-" * 60)
            for metric in result["unique_to_snapshot_a"]:
                print(f"  - {metric}")
    
    if verbose or summary["unique_to_b"] <= 50:
        if result["unique_to_snapshot_b"]:
            print("\n" + "-" * 60)
            print("🟢 METRICS UNIQUE TO SNAPSHOT B (missing in A)")
            print("-" * 60)
            for metric in result["unique_to_snapshot_b"]:
                print(f"  + {metric}")
    
    print("\n" + "=" * 60)


def cmd_list(args):
    """List available snapshots."""
    if not SNAPSHOTS_DIR.exists():
        print("No snapshots directory found", file=sys.stderr)
        return 1
    
    snapshots = sorted(SNAPSHOTS_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    
    if not snapshots:
        print("No snapshots found", file=sys.stderr)
        return 0
    
    print(f"\n{'Snapshot File':<45} {'Version':<15} {'Metrics':<10} {'Date'}")
    print("-" * 90)
    
    for snap_path in snapshots:
        try:
            snap = load_snapshot(snap_path)
            meta = snap.get("metadata", {})
            version = meta.get("platform_version", "?")
            count = meta.get("metrics_count", "?")
            timestamp = meta.get("timestamp", "?")[:19]
            print(f"{snap_path.name:<45} {version:<15} {count:<10} {timestamp}")
        except Exception as e:
            print(f"{snap_path.name:<45} {'ERROR':<15} {'-':<10} {str(e)[:20]}")
    
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Prometheus Metrics Snapshot Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Take a snapshot
  %(prog)s snapshot
  
  # Take a snapshot with custom output
  %(prog)s snapshot -o my-snapshot.yaml
  
  # Compare two snapshots
  %(prog)s compare snapshots/v0.64.0.yaml snapshots/v0.65.0.yaml
  
  # Compare latest snapshot with another
  %(prog)s compare latest snapshots/v0.64.0.yaml
  
  # List all snapshots
  %(prog)s list
"""
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Snapshot command
    snap_parser = subparsers.add_parser("snapshot", help="Take a metrics snapshot")
    snap_parser.add_argument("-o", "--output", help="Output file path")
    snap_parser.add_argument("-u", "--url", help="Prometheus URL (skip port-forward)")
    snap_parser.add_argument("-n", "--namespace", help="Prometheus namespace")
    snap_parser.add_argument("-s", "--service", help="Prometheus service name")
    snap_parser.add_argument("-p", "--port", type=int, help="Prometheus port")
    snap_parser.add_argument("--cluster-path", help="Path to cluster directory (for version info)")
    snap_parser.add_argument("--json", action="store_true", help="Output as JSON")
    
    # Compare command
    cmp_parser = subparsers.add_parser("compare", help="Compare two snapshots")
    cmp_parser.add_argument("snapshot_a", help="First snapshot file (or 'latest')")
    cmp_parser.add_argument("snapshot_b", help="Second snapshot file (or 'latest')")
    cmp_parser.add_argument("--json", action="store_true", help="Output as JSON")
    cmp_parser.add_argument("-v", "--verbose", action="store_true", help="Show all metrics")
    
    # List command
    list_parser = subparsers.add_parser("list", help="List available snapshots")
    
    args = parser.parse_args()
    
    if args.command == "snapshot":
        return cmd_snapshot(args)
    elif args.command == "compare":
        return cmd_compare(args)
    elif args.command == "list":
        return cmd_list(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())