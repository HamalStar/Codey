#pragma once

#include "companion_config.h"

#include <driver/usb_serial_jtag.h>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_websocket_client.h>
#include <freertos/queue.h>
#include <lwip/sockets.h>
#include <ssid_manager.h>

#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <unistd.h>
#include <vector>

namespace {

enum class Transport { None, Usb, WebSocket };

struct LinkCommand {
    int listen = 0; // 1 start, 2 stop, 3 cancel
    uint16_t sequence = 0;
    char session[40]{};
};

struct Transcript {
    char text[sizeof(g_transcript)]{};
    int sequence = 0;
    bool hasSequence = false;
    bool final = false;
    bool pending = false;
};

struct WifiRequest {
    std::string ssid;
    std::string password;
};

struct WifiAttempt {
    std::vector<SsidItem> previous;
    std::string ssid;
    uint32_t started;
    bool enabled;
};

std::atomic<bool> linkRunning;
std::atomic<bool> linkDone{true};
std::atomic<bool> wifiConnected;
std::atomic<bool> usbConnected;
std::atomic<bool> wsReady;
std::atomic<bool> stateReady;
std::atomic<bool> voiceActive;
std::atomic<bool> cancelOnClose;
std::atomic<uint8_t> publishedBrightness;
std::atomic<int> failedVoiceSequence{-1};
QueueHandle_t linkCommands;
std::mutex inboxMutex;
std::unique_ptr<JsonDocument> incomingState;
Transcript incomingTranscript;
std::optional<WifiRequest> wifiRequest;
std::optional<WifiAttempt> wifiAttempt; // UI task owns the factory Wi-Fi mutation.
std::string wifiReply;
std::vector<std::string> publishedHistory;
bool wifiConfigBusy;
uint32_t wifiChecked;

// Transport state is owned by the link task. Only the socket event callback
// uses the atomic signal and its own receive buffer.
esp_websocket_client_handle_t socket;
std::atomic<bool> socketConnected;
std::string wsMessage;
bool usbInstalled;
uint32_t usbLastRx;
std::string remoteAsrUrl;
std::string socketUrl;
uint32_t lastSocketAttempt;
Transport voiceTransport;
std::array<uint8_t, 9600> pendingPcm; // 300 ms of mono, signed 16-bit PCM
size_t pendingSize;

struct LanDiscovery {
    int listener = -1;
    uint32_t started = 0;
    uint32_t lastPoll = 0;
    bool sawBeacon = false;
    bool retrying = false;
    std::string stateUrl;
    std::string asrUrl;

    void closeListener()
    {
        if (listener >= 0) {
            ::close(listener);
            listener = -1;
        }
    }

    void reset(uint32_t now, bool retry = false)
    {
        closeListener();
        started = now;
        lastPoll = now - 50;
        sawBeacon = false;
        retrying = retry;
        stateUrl.clear();
        asrUrl.clear();
    }

    bool poll(uint32_t now)
    {
        if (!CODEY_DISCOVERY_PORT || sawBeacon || now - lastPoll < 50 ||
            (retrying && now - started < 10000)) {
            return false;
        }
        lastPoll = now;
        if (listener < 0) {
            listener = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (listener < 0) {
                return false;
            }
            sockaddr_in address{};
            address.sin_family = AF_INET;
            address.sin_port = htons(CODEY_DISCOVERY_PORT);
            address.sin_addr.s_addr = htonl(INADDR_ANY);
            if (::bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
                closeListener();
                return false;
            }
        }
        char packet[192];
        sockaddr_in sender{};
        socklen_t senderSize = sizeof(sender);
        const int size = recvfrom(listener, packet, sizeof(packet), MSG_DONTWAIT,
                                  reinterpret_cast<sockaddr*>(&sender), &senderSize);
        if (size <= 0 || size >= sizeof(packet) || sender.sin_family != AF_INET) {
            return false;
        }
        JsonDocument beacon;
        if (deserializeJson(beacon, packet, size) || beacon["codey"] != 1 ||
            !beacon["port"].is<uint16_t>() || !beacon["asr"].is<uint16_t>()) {
            return false;
        }
        const auto http = beacon["port"].as<uint16_t>();
        const auto asr = beacon["asr"].as<uint16_t>();
        char ip[INET_ADDRSTRLEN];
        if (!http || !asr || !inet_ntop(AF_INET, &sender.sin_addr, ip, sizeof(ip))) {
            return false;
        }
        stateUrl = std::string("http://") + ip + ":" + std::to_string(http) + "/codey/state";
        asrUrl = std::string("ws://") + ip + ":" + std::to_string(asr) + "/";
        sawBeacon = true;
        closeListener();
        return true;
    }
};

void replyWifi(bool success, const std::string& ssid, const char* message)
{
    JsonDocument reply;
    reply["ok"] = success;
    reply["ssid"] = ssid;
    reply["message"] = message;
    std::lock_guard<std::mutex> lock(inboxMutex);
    wifiReply.clear();
    serializeJson(reply, wifiReply);
    wifiConfigBusy = false;
}

void finishWifiAttempt(bool connected)
{
    if (!wifiAttempt) {
        return;
    }
    auto& hal = GetHAL();
    if (connected) {
        hal.setWifiEnabled(true, true);
    } else {
        // Stop the official station/portal before restoring the credentials
        // that were present when the USB request started.
        hal.setWifiEnabled(false, false);
        auto& credentials = SsidManager::GetInstance();
        credentials.Clear();
        for (auto entry = wifiAttempt->previous.rbegin(); entry != wifiAttempt->previous.rend(); ++entry) {
            credentials.AddSsid(entry->ssid, entry->password);
        }
        if (wifiAttempt->enabled) {
            hal.setWifiEnabled(true, false);
        }
    }
    replyWifi(connected, connected ? wifiAttempt->ssid : hal.getWifiStatus().ssid,
              connected ? "connected" : "connect failed");
    wifiAttempt.reset();
}

void updateWifiAttempt(const Hal::WifiStatus& wifi, uint32_t now)
{
    std::optional<WifiRequest> request;
    {
        std::lock_guard<std::mutex> lock(inboxMutex);
        request.swap(wifiRequest);
    }
    if (request) {
        auto& hal = GetHAL();
        hal.setWifiEnabled(false, false);
        auto& credentials = SsidManager::GetInstance();
        wifiAttempt = WifiAttempt{credentials.GetSsidList(), request->ssid, now, wifi.enabled};
        credentials.AddSsid(request->ssid, request->password);
        const auto& saved = credentials.GetSsidList();
        const auto match = std::find_if(saved.begin(), saved.end(), [&](const auto& item) {
            return item.ssid == request->ssid;
        });
        if (match != saved.end()) {
            credentials.SetDefaultSsid(match - saved.begin());
        }
        hal.setWifiEnabled(true, false);
        return;
    }
    if (wifiAttempt) {
        const bool connected = wifi.connected && wifi.ssid == wifiAttempt->ssid;
        if (connected || now - wifiAttempt->started >= 8000) {
            finishWifiAttempt(connected);
        }
    }
}

void postTranscript(JsonDocument& doc)
{
    Transcript message{};
    copyStr(message.text, sizeof(message.text), doc["text"] | "");
    message.sequence = doc["seq"] | 0;
    message.hasSequence = doc["seq"].is<int>();
    message.final = doc["final"] | false;
    message.pending = true;
    std::lock_guard<std::mutex> lock(inboxMutex);
    if (incomingTranscript.pending && incomingTranscript.final && !message.final &&
        incomingTranscript.sequence == message.sequence) {
        return;
    }
    incomingTranscript = message;
}

bool postState(const char* payload, size_t size, bool trustedRemote = false)
{
    auto doc = std::make_unique<JsonDocument>();
    if (deserializeJson(*doc, payload, size) || !(*doc)["providers"].is<JsonArray>()) {
        return false;
    }
    if (trustedRemote) {
        const char* url = (*doc)["asr_url"] | "";
        remoteAsrUrl = std::strncmp(url, "wss://", 6) == 0 ? url : "";
    }
    {
        std::lock_guard<std::mutex> lock(inboxMutex);
        incomingState.swap(doc);
    }
    stateReady.store(true);
    return true;
}

bool pollState(const char* url)
{
    if (!url[0]) {
        return false;
    }
    esp_http_client_config_t config{};
    config.url = url;
    config.timeout_ms = 1500;
    config.disable_auto_redirect = true;
    config.crt_bundle_attach = esp_crt_bundle_attach;
    auto client = std::unique_ptr<esp_http_client, decltype(&esp_http_client_cleanup)>(
        esp_http_client_init(&config), esp_http_client_cleanup);
    if (!client) {
        return false;
    }
    if (CODEY_REMOTE) {
        esp_http_client_set_header(client.get(), "ngrok-skip-browser-warning", "true");
        if (CODEY_AUTH_HEADER[0]) {
            esp_http_client_set_header(client.get(), "Authorization", CODEY_AUTH_HEADER);
        }
    }
    if (esp_http_client_open(client.get(), 0) != ESP_OK) {
        return false;
    }
    constexpr size_t maxStateSize = 96 * 1024;
    const auto length = esp_http_client_fetch_headers(client.get());
    if (esp_http_client_get_status_code(client.get()) != 200 ||
        length < 0 || length > maxStateSize) {
        return false;
    }
    std::string body;
    body.reserve(length);
    char chunk[512];
    const auto started = millis();
    while (linkRunning.load() && millis() - started < 3500) {
        const int bytes = esp_http_client_read(client.get(), chunk, sizeof(chunk));
        if (bytes < 0 || static_cast<size_t>(bytes) > maxStateSize - body.size()) {
            return false;
        }
        body.append(chunk, bytes);
        if (bytes == 0 || (length > 0 && body.size() == static_cast<size_t>(length))) {
            return esp_http_client_is_complete_data_received(client.get()) &&
                   postState(body.data(), body.size(), CODEY_REMOTE);
        }
    }
    return false;
}

void socketEvent(void*, esp_event_base_t, int32_t event, void* data)
{
    if (event == WEBSOCKET_EVENT_CONNECTED) {
        socketConnected.store(true);
    } else if (event == WEBSOCKET_EVENT_DISCONNECTED) {
        socketConnected.store(false);
        wsReady.store(false);
    } else if (event == WEBSOCKET_EVENT_DATA) {
        const auto& frame = *static_cast<esp_websocket_event_data_t*>(data);
        if (frame.op_code != 1 || frame.payload_len <= 0 || frame.payload_len > 4096 ||
            frame.payload_offset < 0 || frame.data_len < 0 ||
            frame.data_len > frame.payload_len - frame.payload_offset) {
            return;
        }
        if (frame.payload_offset == 0) {
            wsMessage.clear();
        }
        if (wsMessage.size() != static_cast<size_t>(frame.payload_offset)) {
            return;
        }
        wsMessage.append(frame.data_ptr, frame.data_len);
        if (wsMessage.size() == static_cast<size_t>(frame.payload_len)) {
            JsonDocument doc;
            if (!deserializeJson(doc, wsMessage) && std::strcmp(doc["type"] | "", "stt") == 0) {
                postTranscript(doc);
            }
        }
    }
}

void setSocket(std::string_view url)
{
    const auto now = millis();
    if (url == socketUrl && (socket || url.empty() || now - lastSocketAttempt < 3000)) {
        return;
    }
    if (socket) {
        esp_websocket_client_stop(socket);
        esp_websocket_client_destroy(socket);
        socket = nullptr;
    }
    socketConnected.store(false);
    wsReady.store(false);
    socketUrl.assign(url);
    if (url.empty()) {
        return;
    }
    lastSocketAttempt = now;
    esp_websocket_client_config_t config{};
    config.uri = socketUrl.c_str();
    config.headers = CODEY_REMOTE ? CODEY_ASR_HEADERS : nullptr;
    config.crt_bundle_attach = CODEY_REMOTE ? esp_crt_bundle_attach : nullptr;
    config.reconnect_timeout_ms = 3000;
    config.network_timeout_ms = 1500;
    config.buffer_size = 4096;
    socket = esp_websocket_client_init(&config);
    if (socket && (esp_websocket_register_events(socket, WEBSOCKET_EVENT_ANY, socketEvent, nullptr) != ESP_OK ||
                   esp_websocket_client_start(socket) != ESP_OK)) {
        esp_websocket_client_destroy(socket);
        socket = nullptr;
    }
}

bool wsText(const char* text)
{
    const auto size = std::strlen(text);
    return socket && socketConnected.load() &&
           esp_websocket_client_send_text(socket, text, size, pdMS_TO_TICKS(50)) == static_cast<int>(size);
}

void sendListen(Transport channel, int action, uint16_t sequence = 0, const char* session = "")
{
    JsonDocument doc;
    doc["state"] = action == 1 ? "start" : action == 2 ? "stop" : "cancel";
    if (action == 1) {
        doc["mode"] = "manual";
        doc["session"] = session;
        doc["seq"] = sequence;
    }
    if (channel == Transport::WebSocket) {
        doc["type"] = "listen";
    }
    char text[192];
    const auto size = serializeJson(doc, text, sizeof(text));
    if (channel == Transport::Usb) {
        usbSendFrame(U_LISTEN, reinterpret_cast<const uint8_t*>(text), size);
    } else if (channel == Transport::WebSocket) {
        wsText(text);
    }
}

void sendFocus(Transport channel, const char* id)
{
    if (channel == Transport::Usb) {
        usbSendFrame(U_FOCUS, reinterpret_cast<const uint8_t*>(id), std::strlen(id));
    } else if (channel == Transport::WebSocket) {
        JsonDocument doc;
        doc["type"] = "focus";
        doc["session"] = id;
        char text[96];
        serializeJson(doc, text, sizeof(text));
        wsText(text);
    }
}

void flushPcm()
{
    if (pendingSize && voiceTransport == Transport::Usb) {
        usbSendFrame(U_PCM, pendingPcm.data(), pendingSize);
    } else if (pendingSize && voiceTransport == Transport::WebSocket && socket && socketConnected.load()) {
        esp_websocket_client_send_bin(socket, reinterpret_cast<const char*>(pendingPcm.data()),
                                      pendingSize, pdMS_TO_TICKS(50));
    }
    pendingSize = 0;
}

void drainPcm()
{
    if (!g_voiceSB) {
        return;
    }
    for (int i = 0; i < 3; ++i) {
        const auto size = xStreamBufferReceive(g_voiceSB, pendingPcm.data() + pendingSize,
                                               pendingPcm.size() - pendingSize, 0);
        if (size == 0) {
            return;
        }
        if (voiceTransport == Transport::None) {
            continue;
        }
        pendingSize += size;
        if (pendingSize == pendingPcm.size()) {
            flushPcm();
        }
    }
}

void runLink()
{
    usb_serial_jtag_driver_config_t usbConfig{};
    usbConfig.tx_buffer_size = 12288;
    usbConfig.rx_buffer_size = USB_MAX_PAYLOAD + 1024;
    usbInstalled = usb_serial_jtag_driver_install(&usbConfig) == ESP_OK;
    g_rxStage = 0;
    uint32_t lastProbe = millis() - 500;
    uint32_t lastFetch = millis() - 30000;
    LanDiscovery lan;
    lan.reset(millis());
    bool wasWifi = false;
    bool wasUsb = false;
    bool stoppedRecording = false;
    uint16_t activeSequence = 0;

    while (linkRunning.load()) {
        const auto now = millis();
        const bool wifi = wifiConnected.load();
        if (usbInstalled) {
            usbRxPump();
            if (usbConnected.load() && millis() - usbLastRx > 4000) {
                usbConnected.store(false);
            }
            std::string reply;
            {
                std::lock_guard<std::mutex> lock(inboxMutex);
                reply.swap(wifiReply);
            }
            if (!reply.empty()) {
                usbSendFrame(U_CFG_ACK, reinterpret_cast<const uint8_t*>(reply.data()), reply.size());
            }
            if (!usbConnected.load() && usb_serial_jtag_is_connected() && now - lastProbe >= 500) {
                lastProbe = now;
                usbSendFrame(U_HELLO, reinterpret_cast<const uint8_t*>("v1"), 2);
            }
        }

        const bool usb = usbConnected.load();
        if (usb && !wasUsb) {
            usbSendFrame(U_STATE_REQ, nullptr, 0);
        }
        if (wifi != wasWifi || usb != wasUsb) {
            lan.reset(now);
        }
        if (!usb && (wasUsb || (wifi && !wasWifi))) {
            lastFetch = now - 30000;
        }
        if (!wifi && !usb) {
            stateReady.store(false);
        }
        wasUsb = usb;
        wasWifi = wifi;

        if (wifi && !usb && !voiceActive.load() && lan.poll(now)) {
            lastFetch = now - 30000;
        }
        const bool allowFallback = !CODEY_DISCOVERY_PORT || lan.retrying || now - lan.started >= 3000;
        const auto* stateUrl = lan.sawBeacon ? lan.stateUrl.c_str() : allowFallback ? CODEY_STATE_URL : "";
        const auto* asrUrl = lan.sawBeacon ? lan.asrUrl.c_str() : allowFallback ? CODEY_ASR_URL : "";
        // An in-progress voice session stays on its original link.
        if (!wifi || (usb && voiceTransport != Transport::WebSocket)) {
            setSocket("");
        } else if (voiceTransport != Transport::WebSocket) {
            setSocket(CODEY_REMOTE ? std::string_view(remoteAsrUrl) : std::string_view(asrUrl));
        }
        if (socketConnected.load() && !wsReady.load()) {
            wsReady.store(wsText("{\"type\":\"hello\",\"version\":1,\"transport\":\"websocket\","
                                 "\"audio_params\":{\"format\":\"pcm\",\"sample_rate\":16000,\"channels\":1}}"));
        }
        const auto preferred = usb ? Transport::Usb : wsReady.load() ? Transport::WebSocket : Transport::None;
        if ((voiceTransport == Transport::Usb && !usb) ||
            (voiceTransport == Transport::WebSocket && !socketConnected.load())) {
            failedVoiceSequence.store(activeSequence);
            voiceTransport = Transport::None;
            pendingSize = 0;
        }

        LinkCommand command;
        while (xQueueReceive(linkCommands, &command, 0) == pdTRUE) {
            if (command.listen == 1) {
                voiceTransport = preferred;
                activeSequence = command.sequence;
                stoppedRecording = false;
                pendingSize = 0;
                if (voiceTransport == Transport::None) {
                    failedVoiceSequence.store(activeSequence);
                } else {
                    sendListen(voiceTransport, 1, activeSequence, command.session);
                }
            } else if (command.listen == 2) {
                // No producer adds PCM after it queues this control.
                drainPcm();
                flushPcm();
                sendListen(voiceTransport, 2);
                stoppedRecording = true;
            } else if (command.listen == 3) {
                sendListen(voiceTransport, 3);
                pendingSize = 0;
                voiceTransport = Transport::None;
            } else {
                sendFocus(preferred, command.session);
            }
        }
        if (stoppedRecording && !voiceActive.load()) {
            voiceTransport = Transport::None;
        }
        drainPcm();
        if (!usb && wifi && !voiceActive.load() && stateUrl[0] && now - lastFetch >= 30000) {
            lastFetch = now;
            const bool ready = pollState(stateUrl);
            stateReady.store(ready);
            if (!ready && lan.sawBeacon) {
                lan.reset(millis(), true);
                lastFetch = now - 30000;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }

    if (cancelOnClose.load()) {
        sendListen(voiceTransport, 3);
    }
    voiceTransport = Transport::None;
    pendingSize = 0;
    setSocket("");
    if (usbInstalled) {
        usb_serial_jtag_driver_uninstall();
        usbInstalled = false;
    }
    std::string().swap(remoteAsrUrl);
    std::string().swap(socketUrl);
    std::string().swap(wsMessage);
    lan.closeListener();
}

void pollLink(void*)
{
    runLink();
    linkDone.store(true);
    vTaskDelete(nullptr);
}

} // namespace

static void usbOnFrame(uint8_t type, const uint8_t* payload, uint16_t size)
{
    if (type == U_HELLO_ACK) {
        usbLastRx = millis();
        usbConnected.store(true);
        return;
    }
    if (!usbConnected.load()) {
        return;
    }
    usbLastRx = millis();
    if (type == U_STATE) {
        postState(reinterpret_cast<const char*>(payload), size);
    } else if (type == U_STT) {
        JsonDocument doc;
        if (!deserializeJson(doc, payload, size)) {
            postTranscript(doc);
        }
    } else if (type == U_CFG_GET) {
        const auto wifi = GetHAL().getWifiStatus();
        JsonDocument doc;
        doc["ssid"] = wifi.ssid;
        doc["wifi"] = wifi.connected;
        doc["ip"] = wifi.ipAddress;
        doc["bright"] = publishedBrightness.load();
        auto history = doc["history"].to<JsonArray>();
        {
            std::lock_guard<std::mutex> lock(inboxMutex);
            for (const auto& ssid : publishedHistory) {
                auto saved = history.add<JsonObject>();
                saved["ssid"] = ssid;
                saved["count"] = 1;
            }
        }
        std::string out;
        serializeJson(doc, out);
        usbSendFrame(U_CFG_STATE, reinterpret_cast<const uint8_t*>(out.data()), out.size());
    } else if (type == U_CFG_SET) {
        JsonDocument doc;
        const bool valid = size <= 512 && !deserializeJson(doc, payload, size);
        const std::string ssid = valid ? doc["wifi_ssid"] | "" : "";
        const std::string password = valid ? doc["wifi_pass"] | "" : "";
        if (ssid.empty() || ssid.size() > 32 || password.size() > 64) {
            constexpr char error[] = "{\"ok\":false,\"message\":\"Invalid Wi-Fi credentials\"}";
            usbSendFrame(U_CFG_ACK, reinterpret_cast<const uint8_t*>(error), sizeof(error) - 1);
        } else {
            std::lock_guard<std::mutex> lock(inboxMutex);
            if (!wifiConfigBusy) {
                wifiRequest = WifiRequest{ssid, password};
                wifiConfigBusy = true;
            }
        }
    }
}

static void factoryUsbWrite(const uint8_t* head, const uint8_t* payload, uint16_t size, const uint8_t* tail)
{
    if (!usbInstalled || !usb_serial_jtag_is_connected()) {
        return;
    }
    std::vector<uint8_t> frame(size + 7);
    std::memcpy(frame.data(), head, 5);
    if (size) {
        std::memcpy(frame.data() + 5, payload, size);
    }
    std::memcpy(frame.data() + 5 + size, tail, 2);
    usb_serial_jtag_write_bytes(frame.data(), frame.size(), pdMS_TO_TICKS(20));
}

static void factoryUsbRead()
{
    uint8_t buf[512];
    for (int i = 0; i < 8; ++i) {
        const int bytes = usb_serial_jtag_read_bytes(buf, sizeof(buf), 0);
        if (bytes <= 0) {
            break;
        }
        for (int j = 0; j < bytes; ++j) {
            usbRxByte(buf[j]);
        }
    }
}

static void factoryStartLink()
{
    linkCommands = xQueueCreate(8, sizeof(LinkCommand));
    if (!linkCommands) {
        return;
    }
    wifiConnected.store(false);
    usbConnected.store(false);
    wsReady.store(false);
    stateReady.store(false);
    voiceActive.store(false);
    failedVoiceSequence.store(-1);
    wifiChecked = millis() - 500;
    linkRunning.store(true);
    linkDone.store(false);
    if (xTaskCreate(pollLink, "codey_link", 10240, nullptr, 2, nullptr) != pdPASS) {
        linkRunning.store(false);
        linkDone.store(true);
    }
}

static void factoryStopLink(bool cancel)
{
    cancelOnClose.store(cancel);
    linkRunning.store(false);
}
static bool factoryLinkStopped() { return linkDone.load(); }

static void factoryReleaseLink()
{
    finishWifiAttempt(false);
    if (linkCommands) {
        vQueueDelete(linkCommands);
        linkCommands = nullptr;
    }
    std::lock_guard<std::mutex> lock(inboxMutex);
    incomingState.reset();
    incomingTranscript = {};
    wifiRequest.reset();
    wifiReply.clear();
    wifiConfigBusy = false;
    publishedHistory.clear();
}

static void factoryReceiveLink()
{
    const auto now = millis();
    if (now - wifiChecked >= 500) {
        wifiChecked = now;
        const auto wifi = GetHAL().getWifiStatus();
        g_wifi = wifi.connected;
        wifiConnected.store(g_wifi);
        publishedBrightness.store(g_bright);
        copyStr(g_ssid, sizeof(g_ssid), wifi.ssid.c_str());
        updateWifiAttempt(wifi, now);
        if (!wifi.enabled || wifi.connected) {
            std::vector<std::string> history;
            for (const auto& saved : SsidManager::GetInstance().GetSsidList()) {
                history.push_back(saved.ssid);
            }
            std::lock_guard<std::mutex> lock(inboxMutex);
            publishedHistory.swap(history);
        }
    }
    g_usbActive = usbConnected.load();
    g_wsConn = wsReady.load();
    g_companionOk = g_usbActive || stateReady.load();

    std::unique_ptr<JsonDocument> state;
    Transcript transcript;
    {
        std::lock_guard<std::mutex> lock(inboxMutex);
        state.swap(incomingState);
        transcript = incomingTranscript;
        incomingTranscript.pending = false;
    }
    if (state) {
        const int provider = detailProv;
        std::string session;
        if (provider >= 0 && detailIdx >= 0 && detailIdx < PROV[provider].nsess) {
            session = PROV[provider].sess[detailIdx].id;
        }
        applyStateDoc(*state);
        if (!session.empty()) {
            const auto* first = PROV[provider].sess;
            const auto* end = first + PROV[provider].nsess;
            const auto* found = std::find_if(first, end, [&](const auto& entry) { return entry.id == session; });
            detailProv = found == end ? -1 : provider;
            detailIdx = found - first;
        }
        g_haveData = true;
    }
    if (transcript.pending) {
        voiceApplyStt(transcript.text, transcript.final, transcript.sequence, transcript.hasSequence);
    }
    const int failed = failedVoiceSequence.exchange(-1);
    if (g_voice && g_vphase < 3 && failed == g_voiceSeq) {
        copyStr(g_transcript, sizeof(g_transcript), "语音连接已断开");
        g_vphase = 3;
        g_resultT0 = now;
    }
}

static void factorySendControls()
{
    voiceActive.store(g_voice);
    if (!linkCommands) {
        return;
    }
    if (g_netListenReq) {
        LinkCommand command;
        command.listen = g_netListenReq;
        command.sequence = g_voiceSeq;
        copyStr(command.session, sizeof(command.session), g_voiceSid);
        if (xQueueSend(linkCommands, &command, 0) == pdTRUE) {
            g_netListenReq = 0;
        }
    }
    if (g_netFocusReq) {
        LinkCommand command;
        copyStr(command.session, sizeof(command.session), g_focusSid);
        if (xQueueSend(linkCommands, &command, 0) == pdTRUE) {
            g_netFocusReq = false;
        }
    }
}
