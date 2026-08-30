# Multi-stage: compile the static Go binary (no CGO, no external
# runtime dependencies -- internal/arpio and internal/ipc are pure Go,
# no CLI shellouts the way nftables-manager needs `nft`), then run it in
# a minimal image. Everything privileged this process needs
# (CAP_NET_RAW, host networking to see the real LAN interface) is
# granted at the container level, not baked into the image.
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /pp-arp-worker ./cmd/pp-arp-worker

FROM debian:bookworm-slim
COPY --from=build /pp-arp-worker /usr/local/bin/pp-arp-worker
ENTRYPOINT ["/usr/local/bin/pp-arp-worker"]
