#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Runs the app's whole test suite on a throwaway ERPNext v16 bench in rootless podman: MariaDB, Redis and the
# official ERPNext image with this working tree installed as an app on a fresh site. Written for the build
# runners (Zephyrex SOP → Build Runner Budget): podman's root and run dirs are on local /tmp (podman can't use
# NFS), every container uses the host network (the runner CTs have no /dev/net/tun), and MariaDB, Redis and the
# desk listen on three ports from the app's block.
#
#   tools/test-bench.sh up           load the image archives in $BENCH_IMAGES (else podman pulls), build the bench
#   tools/test-bench.sh test         copy the working tree in again, migrate and run the suite
#   tools/test-bench.sh test --no-migrate   the same without the migrate (just after `up`, which installed it)
#   tools/test-bench.sh serve        serve the site's desk on http://127.0.0.1:$((BENCH_PORT_BASE + 2))
#   tools/test-bench.sh down         remove the containers and the podman root
#
# Settings: BENCH_PREFIX (names, default frappe-auction-test), BENCH_PORT_BASE (default 48500: MariaDB, Redis,
# desk), BENCH_PODMAN_ROOT (default /tmp/$BENCH_PREFIX), BENCH_IMAGES (directories of `podman load` archives,
# colon-separated; every *.tar in them is loaded; on the runners /var/cache/runner/images/erpnext-v16.36.1 and
# /var/cache/runner/images/erpnext-v16.36.1-payments-cca07d9), BENCH_ERPNEXT_IMAGE (default the official ERPNext
# image; localhost/erpnext-payments:v16.36.1-cca07d9 is the same with the payments app in it).
main() {
	set -euo pipefail
	local root prefix podman_root db redis bench site port_base db_port redis_port serve_port
	root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
	prefix="${BENCH_PREFIX:-frappe-auction-test}"
	podman_root="${BENCH_PODMAN_ROOT:-/tmp/$prefix}"
	db="$prefix-db"
	redis="$prefix-redis"
	bench="$prefix-bench"
	site="auction.test"
	port_base="${BENCH_PORT_BASE:-48500}"
	db_port="$port_base"
	redis_port="$((port_base + 1))"
	serve_port="$((port_base + 2))"
	local erpnext_image="${BENCH_ERPNEXT_IMAGE:-docker.io/frappe/erpnext:v16.36.1}"
	local db_image="docker.io/library/mariadb:11.8"
	local redis_image="docker.io/library/redis:7-alpine"
	local db_root_password="bench-root"
	local admin_password="bench-admin"
	local db_ready_attempts=120
	local app_dir="/home/frappe/frappe-bench/apps/auction"
	# Payment holds go through the payments app's gateways; the ERPNext image doesn't ship it.
	local payments_repo="https://github.com/frappe/payments"
	local payments_branch="version-16"

	pod() {
		podman --root "$podman_root/storage" --runroot "$podman_root/run" "$@"
	}

	in_bench() {
		pod exec -i -w /home/frappe/frappe-bench "$bench" "$@"
	}

	# The working tree (tracked and untracked files, nothing ignored) replaces the app's source in the bench.
	copy_app() {
		pod exec -u root "$bench" mkdir -p "$app_dir"
		git -C "$root" ls-files -z --cached --others --exclude-standard \
			| tar -C "$root" --null -T - -cf - \
			| pod exec -i -u root "$bench" tar -C "$app_dir" -xf -
		pod exec -u root "$bench" chown -R frappe:frappe "$app_dir"
	}

	mkdir -p "$podman_root"
	export TMPDIR="$podman_root/tmp"
	mkdir -p "$TMPDIR"

	case "${1:-}" in
	up)
		local images_dir archive
		local -a image_dirs
		if [ -n "${BENCH_IMAGES:-}" ]; then
			IFS=: read -ra image_dirs <<<"$BENCH_IMAGES"
			for images_dir in "${image_dirs[@]}"; do
				[ -e "$images_dir/.DONE" ] || { echo "no published image set at $images_dir" >&2; exit 1; }
				for archive in "$images_dir"/*.tar; do
					pod load --quiet -i "$archive" >/dev/null
				done
			done
		fi
		pod run -d --name "$db" --network host -e MARIADB_ROOT_PASSWORD="$db_root_password" "$db_image" \
			--port="$db_port" --bind-address=127.0.0.1 \
			--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci \
			--skip-character-set-client-handshake >/dev/null
		pod run -d --name "$redis" --network host "$redis_image" \
			redis-server --port "$redis_port" --bind 127.0.0.1 --save "" >/dev/null
		pod run -d --name "$bench" --network host --entrypoint sleep "$erpnext_image" infinity >/dev/null
		local attempt
		for attempt in $(seq "$db_ready_attempts"); do
			if pod exec "$db" mariadb-admin ping -h 127.0.0.1 -P "$db_port" --protocol=tcp -uroot \
				-p"$db_root_password" --silent >/dev/null 2>&1; then
				break
			fi
			if [ "$attempt" = "$db_ready_attempts" ]; then
				echo "MariaDB never became ready" >&2
				exit 1
			fi
			sleep 1
		done
		copy_app
		in_bench bench set-config -g db_host 127.0.0.1
		in_bench bench set-config -gp db_port "$db_port"
		in_bench bench set-config -g redis_cache "redis://127.0.0.1:$redis_port/0"
		in_bench bench set-config -g redis_queue "redis://127.0.0.1:$redis_port/1"
		in_bench bench set-config -g redis_socketio "redis://127.0.0.1:$redis_port/2"
		in_bench ./env/bin/python -m pip install --quiet -e apps/auction
		if ! in_bench test -d apps/payments; then
			in_bench bench get-app --skip-assets --branch "$payments_branch" payments "$payments_repo"
		fi
		# sites/ is a volume, so an image with payments baked in doesn't carry it in apps.txt.
		in_bench bash -c 'grep -qx payments sites/apps.txt || printf "\npayments\n" >> sites/apps.txt'
		# The image's apps.txt has no final newline.
		in_bench bash -c 'grep -qx auction sites/apps.txt || printf "\nauction\n" >> sites/apps.txt'
		in_bench bench new-site "$site" --mariadb-root-password "$db_root_password" \
			--admin-password "$admin_password" --mariadb-user-host-login-scope='%' --install-app erpnext
		in_bench bench --site "$site" install-app payments
		in_bench bench --site "$site" install-app auction
		in_bench bench --site "$site" set-config allow_tests true
		;;
	test)
		copy_app
		if [ "${2:-}" != "--no-migrate" ]; then
			in_bench bench --site "$site" migrate
		fi
		in_bench bench --site "$site" run-tests --app auction
		;;
	serve)
		# The image keeps its built assets beside sites/, which is a volume, so the link to them is missing.
		in_bench ln -sfn /home/frappe/frappe-bench/assets sites/assets
		# A fresh site sends every System Manager to the setup wizard; mark it set up so the desk opens.
		in_bench bench --site "$site" console <<-'PYTHON' >/dev/null
			frappe.db.sql("update `tabInstalled Application` set is_setup_complete = 1")
			frappe.db.set_default("setup_complete", 1)
			frappe.db.set_default("desktop:home_page", "desktop")
			frappe.db.commit()
			frappe.clear_cache()
		PYTHON
		in_bench bench use "$site"
		in_bench bench serve --port "$serve_port"
		;;
	down)
		# The bench container's `sleep` ignores SIGTERM, so don't wait for it.
		pod rm -af -t 0 >/dev/null || true
		# Overlay mounts live in rootless podman's own mount namespace, so they are listed and unmounted there.
		pod unshare sh -c "grep -o '$podman_root[^ ]*' /proc/mounts | sort -r | xargs -r -n1 umount" || true
		pod unshare rm -rf "$podman_root" 2>/dev/null || true
		rm -rf "$podman_root"
		[ ! -e "$podman_root" ]
		;;
	*)
		echo "usage: $0 up|test [--no-migrate]|serve|down" >&2
		exit 2
		;;
	esac
}
main "$@"
