# Thin wrapper around the official image, adding only an automated
# first-run bootstrap (see entrypoint.sh) -- the AdGuard Home binary,
# its Alpine base, and everything else about the image are untouched.
FROM adguard/adguardhome:v0.107.79

COPY adguard/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
