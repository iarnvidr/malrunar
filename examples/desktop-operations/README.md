# Desktop operations examples

Tested helpers for consumers of `desktop-base`; the explanation is in
[`docs/desktop-operations.md`](../../docs/desktop-operations.md).

- `clean-stale-x-locks.sh`: clears the X lock and socket files of an earlier xrdp desktop (`:10`-`:19`) so a
  restarted container gets its desktop back on `:10`. Run it before `xrdp-sesman` starts.
- `xorgxrdp-gpu.conf`: the xorgxrdp `Device` section that also trusts `virtio_gpu`, for VMs that expose
  their host GPU through virtio-gpu (virgl).
