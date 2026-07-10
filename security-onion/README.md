# security-onion: SO-host customizations

Two small files that live on the Security Onion box itself, versioned here because your
config-versioning of the Docker host won't capture the SO guest filesystem, and you will
absolutely not remember either of these fixes when you rebuild that VM in a year. Install
them on the SO manager (e.g. `ssh <you>@$SOC_SO_IP`, with sudo).

Both are optional hardening/observability add-ons referenced by the autonomous cycle and
the storage-resilience notes; neither is required to run the gateway or skill.

## salt-local/ntp/chrony.conf: guest-clock auto-resync after a hypervisor pause

If SO runs as a VM, a hypervisor pause (e.g. on a host-disk-full event) freezes the guest
clock. chrony's default `makestep 1.0 3` only steps the clock in the first 3 updates
after start, so a mid-run pause never gets corrected. The guest clock can drift hours
behind, which silently back-timestamps all ingest, so every `now-X` query and the
scheduled cycle miss "today".

This override sets `makestep 1.0 -1` (step at any poll when offset > 1s) so the clock
self-corrects within one NTP poll after resume. `/etc/chrony.conf` is salt-managed, so
this is an SO-local override (file_roots lists `local/salt` before `default/salt`;
survives `soup`/highstate).

```bash
sudo cp chrony.conf /opt/so/saltstack/local/salt/ntp/chrony.conf
sudo salt-call state.apply ntp
timedatectl show -p NTPSynchronized --value   # -> yes   (note: with cmdport 0, chronyc can't talk to the daemon)
```

## rule-update-health/: a monitor for the daily signature update

`so-rule-update` (idstools-rulecat) refreshes Suricata ET rules daily (about 07:01).
Nothing watches it by default, so a failed or stale run is silent. This monitor evaluates
the run log read-only and writes one ES doc (`so-rule-update-health/_doc/latest`, fixed id
so the index never grows) reporting `status` / `age_hours` / `final_write_present` /
`rules_total` / `error_count`. The autonomous cycle reads that doc via the elasticsearch
MCP and reports signature freshness (see the cycle's §2).

It uses SO's own ES `curl.config`, so no new secrets. It installs to `/etc/cron.d` (not
`/var/spool/cron`, which SO manages) so it survives `soup` and reboots.

```bash
sudo install -m 0755 -o root -g root rule-update-health/so-rule-update-healthcheck.sh /usr/local/bin/
sudo install -m 0644 rule-update-health/so-rule-update-health.cron /etc/cron.d/so-rule-update-health
sudo /usr/local/bin/so-rule-update-healthcheck.sh   # seed the doc
```

> The healthcheck script hardcodes standard SO paths
> (`/opt/so/log/idstools/download_cron.log`, `/opt/so/conf/elasticsearch/curl.config`,
> `https://localhost:9200`) that are consistent across SO 2.4 installs. Adjust only if
> your SO layout differs.
