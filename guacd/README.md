# guacd

Standalone Apache Guacamole proxy daemon image. Runs as the unprivileged `guacd` account (UID/GID 1000), with `HOME=/home/guacd`; that directory is created and owned by the account. The default home is writable even with a read-only root filesystem. If using a tmpfs for `/tmp`, mount it writable for UID 1000 (for example `--tmpfs /tmp:rw,nosuid,nodev`). The image does not require writable root or `/tmp` for its normal daemon operation.

The daemon listens on `0.0.0.0:4822`; it has no authentication. Keep the port on a trusted network or bind it to loopback in a derived configuration. Builds and pins are described in the Dockerfile and `pins.env`.

## Runtime compatibility

The published image defaults to UID 1000 and `/home/guacd`. Container orchestrators that override the user must ensure the home remains writable; numeric UID 1000 is the supported default. The smoke test checks that HOME is writable with a read-only root and a writable `/tmp` tmpfs, as UID 1000.
