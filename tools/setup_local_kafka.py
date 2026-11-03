"""Download verified Apache Kafka and prepare a loopback-only KRaft broker."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.request
import uuid
import base64

ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.4.0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", required=True, type=Path)
    args = parser.parse_args()
    directory = ROOT / "runtime-verification" / "kafka"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"kafka_2.13-{VERSION}"
    archive = directory / f"{name}.tgz"
    url = f"https://downloads.apache.org/kafka/{VERSION}/{name}.tgz"
    if not archive.exists():
        print(f"Downloading official Apache Kafka {VERSION}", flush=True)
        urllib.request.urlretrieve(url, archive)
    checksum = urllib.request.urlopen(url + ".sha512", timeout=30).read().decode()
    expected = re.search(r"\b[0-9a-fA-F]{128}\b", checksum)
    if expected is None or hashlib.file_digest(archive.open("rb"), "sha512").hexdigest().lower() != expected.group().lower():
        raise RuntimeError("Apache SHA-512 verification failed")
    if not (directory / name / "libs").exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(directory, filter="data")
    home = directory / name
    data = directory / "data"
    properties = directory / "server.properties"
    properties.write_text("\n".join([
        "process.roles=broker,controller", "node.id=1",
        "controller.quorum.bootstrap.servers=127.0.0.1:19093",
        "listeners=PLAINTEXT://127.0.0.1:19092,CONTROLLER://127.0.0.1:19093",
        "advertised.listeners=PLAINTEXT://127.0.0.1:19092",
        "listener.security.protocol.map=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT",
        "controller.listener.names=CONTROLLER", "inter.broker.listener.name=PLAINTEXT",
        f"log.dirs={data.as_posix()}", "num.partitions=3",
        "offsets.topic.replication.factor=1", "transaction.state.log.replication.factor=1",
        "transaction.state.log.min.isr=1", "group.initial.rebalance.delay.ms=0",
        "auto.create.topics.enable=true", "log.retention.hours=24", "",
    ]), encoding="utf-8")
    common = [str(args.java.resolve()), "-Xms256m", "-Xmx512m", "-cp", str(home / "libs" / "*")]
    if not (data / "meta.properties").exists():
        cluster = base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
        subprocess.run([*common, "kafka.tools.StorageTool", "format", "--standalone", "-t", cluster, "-c", str(properties)], check=True)
    config = {"version":VERSION, "sha512_verified":True, "java":str(args.java.resolve()),
              "command":[*common, "kafka.Kafka", str(properties)], "bootstrap_servers":"127.0.0.1:19092"}
    (directory / "launch.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(json.dumps({key:value for key,value in config.items() if key != "command"}), flush=True)


if __name__ == "__main__":
    main()
