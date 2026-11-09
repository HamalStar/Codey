// sketches/codey_dash/codey_discover.h — 局域网自动发现 Companion(免填 IP)。
// Companion 每 2s 从各网卡广播 UDP 信标 {"codey":1,"port":8787,"asr":8788,"host":"..."} 到 :8789;
// 这里在 resolveMac 时短暂监听,取第一个合法信标的「源 IP」作为 Companion 地址。
// 自包含(只依赖 WiFi/WiFiUdp/ArduinoJson),移植到其它固件只需带上本头并在找主机处调用 discoverCompanion()。
#pragma once
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>

static const uint16_t DISCOVERY_PORT = 8789;      // 与 companion CODEY_DISCOVERY_PORT 一致

// 阻塞监听最多 waitMs;收到信标返回 true 并写出 Companion IP。只在 netTask(core0)里调用。
static bool discoverCompanion(String& ipOut, uint32_t waitMs = 3000) {
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiUDP udp;
  if (!udp.begin(DISCOVERY_PORT)) return false;
  bool found = false;
  uint32_t t0 = millis();
  while (!found && millis() - t0 < waitMs) {
    if (udp.parsePacket() > 0) {
      char buf[192];
      int n = udp.read(buf, sizeof(buf) - 1);
      if (n > 0) {
        buf[n] = 0;
        JsonDocument d;
        if (!deserializeJson(d, buf) && (d["codey"] | 0) == 1) {
          ipOut = udp.remoteIP().toString();
          Serial.printf("[discover] companion %s (%s)\n", ipOut.c_str(), (const char*)(d["host"] | "?"));
          found = true;
        }
      }
    } else {
      delay(50);
    }
  }
  udp.stop();
  return found;
}
