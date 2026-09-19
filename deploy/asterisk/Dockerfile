# Asterisk 22 built from source with chan_websocket enabled.
#
# Ubuntu's asterisk-modules package (22.5.2) ships res_websocket_client and
# res_http_websocket but NOT chan_websocket, which Dograh's ARI external-media
# path hardcodes (transport="websocket"). So we compile from source and enable
# it via menuselect. Everything else in the deployment is unchanged.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# Build + runtime dependencies. pjproject and jansson are built bundled, so no
# system packages for those; the rest are Asterisk's core build prereqs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential wget ca-certificates pkg-config \
      libedit-dev libjansson-dev libsqlite3-dev libxml2-dev libncurses-dev \
      uuid-dev libssl-dev libcurl4-openssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN wget -q https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22-current.tar.gz \
 && tar xf asterisk-22-current.tar.gz \
 && rm asterisk-22-current.tar.gz \
 && mv asterisk-22.* asterisk

WORKDIR /usr/src/asterisk
RUN ./configure --with-jansson-bundled --with-pjproject-bundled

# Generate menuselect options, then explicitly enable the websocket modules
# Dograh needs. chan_websocket is the one Ubuntu omitted.
RUN make menuselect.makeopts \
 && menuselect/menuselect \
      --enable chan_websocket \
      --enable res_websocket_client \
      --enable res_http_websocket \
      menuselect.makeopts

RUN make -j"$(nproc)" \
 && make install \
 && make samples \
 && ldconfig

# Confirm chan_websocket actually built — fail the image build if not, so we
# never ship a broken Asterisk.
RUN test -f /usr/lib/asterisk/modules/chan_websocket.so \
 || test -f /usr/local/lib/asterisk/modules/chan_websocket.so \
 || (echo "FATAL: chan_websocket.so was not built" && exit 1)

CMD ["asterisk", "-f", "-vvv"]
