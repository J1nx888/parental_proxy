# Multi-stage: compile the static Go binary, then run it in a minimal
# image that ALSO has the real `nft` CLI installed -- knftables
# (sigs.k8s.io/knftables) shells out to it rather than talking netlink
# directly, confirmed live 2026-08-30 ("could not find nftables binary:
# exec: \"nft\": executable file not found in $PATH") while first
# verifying this binary against a real kernel.
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /pp-nftables-manager ./cmd/pp-nftables-manager

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends nftables \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /pp-nftables-manager /usr/local/bin/pp-nftables-manager

# Runs as root deliberately -- CAP_NET_ADMIN alone isn't enough to
# create/manage the "parental_proxy" nftables table's rules the way
# this process needs (confirmed live this session for the analogous
# Squid case: a non-root effective user loses a capability granted at
# the container level unless the binary itself carries a matching file
# capability -- simplest correct answer here, since this process's
# whole purpose IS privileged nftables management with nothing else
# running alongside it in this container, is to just run it as root).
ENTRYPOINT ["/usr/local/bin/pp-nftables-manager"]
