#!/bin/sh
# Install the server-compatible client from PostgreSQL's signed APT repository.
set -eu
. /etc/os-release
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl
install -d /usr/share/postgresql-common/pgdg
curl --fail --silent --show-error \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' \
  "$VERSION_CODENAME" > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y --no-install-recommends postgresql-client-16
/usr/lib/postgresql/16/bin/pg_dump --version
