"""Standalone Panel: NGINX site, Panel and Wings certificates and renewal units."""
import urllib.parse
import urllib.request

from .common import atomic_write


class NativeWebMixin:
    def nginx_config(self, bootstrap=False):
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        listen = self.s.get("bind", "0.0.0.0") + ":" + str(self.s["http_port"])
        challenge = f"location ^~ /.well-known/acme-challenge/ {{ root {self.acme}; default_type text/plain; }}\n"
        if bootstrap:
            return f"server {{ listen {listen}; server_name {host}; {challenge} location / {{ return 503; }} }}\n"
        headers = ('add_header X-Content-Type-Options nosniff always;\n'
                   'add_header X-Frame-Options DENY always;\n'
                   'add_header Referrer-Policy same-origin always;\n')
        application = (f"root {self.app}/public;\nindex index.php;\nclient_max_body_size 100m;\n{headers}"
                       "location / { try_files $uri $uri/ /index.php?$query_string; }\n"
                       'location ^~ /assets/extensions/ { add_header Content-Security-Policy "sandbox" always; add_header X-Content-Type-Options nosniff always; location ~ /\\. { deny all; } location ~ \\.m?js$ { add_header X-Content-Type-Options nosniff always; try_files $uri =404; } try_files $uri =404; }\n'
                       'location ^~ /assets/ { location ~ /\\. { deny all; } try_files $uri =404; }\n'
                       'location ~ \\.php$ { try_files $uri =404; include fastcgi_params; fastcgi_pass unix:/run/php/ptero-installer.sock; fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name; fastcgi_param HTTP_PROXY ""; fastcgi_read_timeout 300; }\n'
                       'location ~ /\\. { deny all; }\n')
        if not self.s["tls"]:
            return f"server {{ listen {listen}; server_name {host}; {challenge}{application} }}\n"
        cert = self.certdir / "live" / host
        return (f"server {{ listen {listen}; server_name {host}; {challenge} location / {{ return 301 https://$host$request_uri; }} }}\n"
                f"server {{ listen {self.s.get('bind', '0.0.0.0')}:{self.s.get('https_port', 443)} ssl; server_name {host};\n"
                f"ssl_certificate {cert}/fullchain.pem;\nssl_certificate_key {cert}/privkey.pem;\n"
                f"ssl_protocols TLSv1.2 TLSv1.3;\n{application} }}\n")

    def publish_site(self, bootstrap=False):
        atomic_write(self.site, self.nginx_config(bootstrap), 0o644)
        self.enabled.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if not self.enabled.exists():
            self.enabled.symlink_to(self.site)
        self.run.run(["nginx", "-t"], label="Validating NGINX configuration")
        self.run.run(["systemctl", "enable", "--now", "nginx"])
        self.run.run(["systemctl", "reload", "nginx"])

    def certificate(self):
        if not self.s["tls"]:
            return
        for directory in (self.acme, self.acme / ".well-known", self.acme / ".well-known/acme-challenge"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            directory.chmod(0o755)
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        self.publish_site(bootstrap=True)
        self.run.run(["certbot", "certonly", "--webroot", "-w", self.acme, "--config-dir", self.certdir,
                      "--work-dir", self.p.data / "acme-work", "--logs-dir", self.p.data / "acme-logs",
                      "--non-interactive", "--agree-tos", "--email", self.s["email"], "-d", host],
                     label="Issuing the Panel HTTPS certificate", umask=0o022)
        self.renewal_units()

    def renewal_units(self):
        if not self.s["tls"]:
            return
        atomic_write(self.units / "ptero-panel-cert.service",
                     f"[Service]\nType=oneshot\nExecStart=/usr/bin/certbot renew --config-dir {self.certdir} --work-dir {self.p.data}/acme-work --logs-dir {self.p.data}/acme-logs --quiet --deploy-hook \"systemctl reload nginx\"\n", 0o644)
        atomic_write(self.units / self.cert_timer,
                     "[Unit]\nDescription=Renew Pterodactyl Panel certificates\n[Timer]\nOnCalendar=*-*-* 03,15:30:00\nRandomizedDelaySec=1800\nPersistent=true\n[Install]\nWantedBy=timers.target\n", 0o644)
        self.run.run(["systemctl", "daemon-reload"])
        self.run.run(["systemctl", "enable", "--now", self.cert_timer])

    def wings_tls(self):
        from .core import valid_domain, valid_email
        domain = self.ui.ask("Wings domain / subdomain", validator=valid_domain)
        email = self.ui.ask("Let's Encrypt email", self.s["email"], valid_email)
        self.ui.confirm("ISSUE WINGS SSL")
        for directory in (self.acme, self.acme / ".well-known", self.acme / ".well-known/acme-challenge"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            directory.chmod(0o755)
        site = self.site.with_name("ptero-wings-acme.conf")
        enabled = self.enabled.with_name(site.name)
        host = urllib.parse.urlsplit(self.s["url"]).hostname
        # Same hostname is already served by the Panel's ACME location.
        if domain != host:
            atomic_write(site, f"server {{ listen 80; server_name {domain}; location ^~ /.well-known/acme-challenge/ {{ root {self.acme}; }} location / {{ return 404; }} }}\n", 0o644)
            if not enabled.exists():
                enabled.symlink_to(site)
        self.run.run(["nginx", "-t"])
        self.run.run(["systemctl", "reload", "nginx"])
        atomic_write(self.p.hook, self.i.wings_renew_hook(), 0o700)
        self.run.run(["certbot", "certonly", "--webroot", "-w", self.acme, "--non-interactive", "--agree-tos",
                      "--email", email, "-d", domain], label="Issuing the Wings HTTPS certificate", umask=0o022)
        self.run.run(["systemctl", "enable", "--now", "certbot.timer"])
        self.ui.say("Wings certificate is ready. Synchronize the node configuration after changing SSL settings.", "ok")
