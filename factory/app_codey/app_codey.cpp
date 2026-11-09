#include "app_codey.h"

#define CODEY_FACTORY 1
#include "sketch/codey_dash.ino"
#include "codey_factory_link.h"

#include <mooncake_templates.h>
#include <nvs.h>

namespace {

constexpr int icon_size = 96;
constexpr auto make_icon()
{
    std::array<uint32_t, icon_size * icon_size> pixels{};
    for (int y = 0; y < icon_size; ++y) {
        for (int x = 0; x < icon_size; ++x) {
            const int dx = x - 48;
            const int dy = y - 48;
            if (dx * dx + dy * dy <= 44 * 44) {
                pixels[y * icon_size + x] = 0xFF202D3A;
            }
            const int abs_y = dy < 0 ? -dy : dy;
            const int diagonal = x + abs_y;
            if (x >= 24 && abs_y <= 16 && diagonal >= 42 && diagonal <= 49) {
                pixels[y * icon_size + x] = 0xFFFFAA80;
            }
            if (x >= 51 && x < 73 && y >= 58 && y < 65) {
                pixels[y * icon_size + x] = 0xFF78E1CC;
            }
        }
    }
    return pixels;
}

constexpr auto icon_pixels = make_icon();
const lv_image_dsc_t icon = [] {
    lv_image_dsc_t image{};
    image.header.magic = LV_IMAGE_HEADER_MAGIC;
    image.header.cf = LV_COLOR_FORMAT_ARGB8888;
    image.header.w = icon_size;
    image.header.h = icon_size;
    image.header.stride = icon_size * sizeof(uint32_t);
    image.data_size = sizeof(icon_pixels);
    image.data = reinterpret_cast<const uint8_t*>(icon_pixels.data());
    return image;
}();

struct AudioFrame {
    uint16_t sequence;
    int16_t pcm[STREAM_CHUNK];
};

QueueHandle_t audioFrames;
TaskHandle_t audioWorker;
std::atomic<bool> audioRunning;
std::atomic<bool> audioDone{true};
std::atomic<int> audioSequence{-1};
bool openWifiSetup;
bool canvasReady;
int originalBrightness;
Hal::ButtonConfig originalButtonConfig;
long companionEpoch;
uint32_t companionEpochAt;

void recordAudio(void*)
{
    std::vector<int16_t> input;
    TickType_t nextChunk = xTaskGetTickCount();
    while (audioRunning.load()) {
        const int sequence = audioSequence.load();
        if (sequence < 0) {
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(50));
            nextChunk = xTaskGetTickCount();
            continue;
        }

        // The factory codec stays at its normal sample rate. A 30 ms chunk
        // yields an integral number of samples at both 44.1 kHz and 16 kHz.
        GetHAL().audioRecord(input, 30, 18.0f);
        if (input.size() < STREAM_CHUNK) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (!audioRunning.load() || audioSequence.load() != sequence) {
            continue;
        }

        AudioFrame frame{};
        frame.sequence = static_cast<uint16_t>(sequence);
        for (size_t i = 0; i < STREAM_CHUNK; ++i) {
            const size_t first = i * input.size() / STREAM_CHUNK;
            const size_t end = (i + 1) * input.size() / STREAM_CHUNK;
            int32_t sum = 0;
            for (size_t j = first; j < end; ++j) {
                sum += input[j];
            }
            frame.pcm[i] = static_cast<int16_t>(sum / static_cast<int32_t>(end - first));
        }
        xQueueSend(audioFrames, &frame, 0);
        if (xTaskDelayUntil(&nextChunk, pdMS_TO_TICKS(30)) == pdFALSE) {
            // A full DMA queue can make reads complete immediately. Always
            // yield, and restart the cadence rather than catching up in a loop.
            vTaskDelay(1);
            nextChunk = xTaskGetTickCount();
        }
    }
    // Destroy the vector before deleting the FreeRTOS task.
    std::vector<int16_t>().swap(input);
    audioDone.store(true);
    vTaskDelete(nullptr);
}

void stopWorkers()
{
    audioSequence.store(-1);
    audioRunning.store(false);
    factoryStopLink(g_vphase != 3 || !g_sttFinal);
}

void loadPrefs()
{
    g_bright = static_cast<uint8_t>((originalBrightness * 255 + 50) / 100);
    g_volume = static_cast<uint8_t>(GetHAL().getSpeakerVolume());
    nvs_handle_t prefs;
    if (nvs_open("codey", NVS_READONLY, &prefs) == ESP_OK) {
        nvs_get_u8(prefs, "bright", &g_bright);
        nvs_get_u8(prefs, "vol", &g_volume);
        nvs_close(prefs);
    }
    g_volume = std::min<uint8_t>(g_volume, 100);
}

} // namespace

static void factorySetCompanionTime(long epoch)
{
    if (epochSane(epoch)) {
        companionEpoch = epoch;
        companionEpochAt = millis();
    }
}

static long factoryEpoch()
{
    return companionEpoch ? companionEpoch + (millis() - companionEpochAt) / 1000 : time(nullptr);
}

static void factoryOpenWifiSetup() { openWifiSetup = true; }

static void factoryPlayChime(bool claude, uint8_t volume)
{
    const auto rate = GetHAL().getAudioSampleRate();
    if (rate <= 0 || volume == 0) {
        return;
    }
    std::vector<int16_t> pcm(rate * 240 / 1000);
    const int tone = rate * 100 / 1000;
    const int second = rate * 130 / 1000;
    const float firstFrequency = claude ? 880.f : 660.f;
    for (int i = 0; i < tone; ++i) {
        const float envelope = static_cast<float>(std::min({i, tone - i, rate / 100})) / (rate / 100);
        const float t = (6.2831853f * firstFrequency * i) / rate;
        const float amplitude = 120.f * volume * envelope;
        pcm[i] = static_cast<int16_t>(sinf(t) * amplitude);
        pcm[second + i] = static_cast<int16_t>(sinf(t * 1.5f) * amplitude);
    }
    GetHAL().audioPlay(pcm, true);
}

static void factorySavePrefs(uint8_t brightness, uint8_t volume)
{
    nvs_handle_t prefs;
    if (nvs_open("codey", NVS_READWRITE, &prefs) == ESP_OK) {
        nvs_set_u8(prefs, "bright", brightness);
        nvs_set_u8(prefs, "vol", volume);
        nvs_commit(prefs);
        nvs_close(prefs);
    }
}

static void factoryBeginAudio(uint16_t sequence)
{
    if (!audioFrames) {
        return;
    }
    xQueueReset(audioFrames);
    audioSequence.store(sequence);
    xTaskNotifyGive(audioWorker);
}

static bool factoryReadAudio(uint16_t sequence, int16_t* pcm)
{
    AudioFrame frame;
    while (xQueueReceive(audioFrames, &frame, 0) == pdTRUE) {
        if (frame.sequence == sequence) {
            std::memcpy(pcm, frame.pcm, sizeof(frame.pcm));
            return true;
        }
    }
    return false;
}

AppCodey::AppCodey()
{
    setAppInfo().name = "Codey";
    setAppInfo().icon = const_cast<lv_image_dsc_t*>(&icon);
}

void AppCodey::onOpen()
{
    _keys = {};
    _closing = false;
    openWifiSetup = false;
    originalBrightness = GetHAL().getBackLightBrightness();
    originalButtonConfig = GetHAL().getButtonConfig();
    // The stock button handler plays a tone and vibrates before reporting a
    // press. Keep that feedback out of the mic path, including both B edges.
    GetHAL().setButtonConfig({false, false});
    loadPrefs();

    GetHAL().stopLvglUpdate();
    { LvglLockGuard barrier; }

    auto& display = GetHAL().getDisplay();
    display.fillScreen(0);
    auto createCanvas = [](M5Canvas& sprite) {
        sprite.setColorDepth(16);
        sprite.setPsram(true);
        return sprite.createSprite(SIZE, SIZE) != nullptr;
    };
    canvasReady = createCanvas(cv);
    if (!canvasReady) {
        display.setTextDatum(middle_center);
        display.drawString("Codey display unavailable", CX, CY);
        return;
    }
    g_ringAok = createCanvas(g_ringA);
    g_ringBok = createCanvas(g_ringB);
    g_arcPct[0] = g_arcPct[1] = -1;
    g_voiceBgOk = createCanvas(g_voiceBg);
    g_voiceBgDirty = true;
    codeySetBrightness(g_bright);

    g_voiceSB = xStreamBufferCreate(16384, 1);
    audioFrames = xQueueCreate(5, sizeof(AudioFrame));
    audioRunning.store(g_voiceSB && audioFrames);
    audioDone.store(!audioRunning.load());
    if (audioRunning.load() &&
        xTaskCreatePinnedToCore(recordAudio, "codey_mic", 4096, nullptr, 2, &audioWorker, 1) != pdPASS) {
        audioRunning.store(false);
        audioDone.store(true);
    }
    g_micOK = audioRunning.load();
    factoryStartLink();

    page = 0;
    detailProv = -1;
    g_listView = g_inSettings = g_dim = false;
    g_voice = false;
    g_tDown = false;
    g_touchLostMs = 0;
    g_vphase = 0;
    g_netListenReq = 0;
    g_netFocusReq = false;
    g_forceRender = true;
    g_haveData = false;
    g_companionOk = false;
    g_wsConn = false;
    g_usbActive = false;
    g_chimeSynced = false;
    bootMs = lastActiveMs = millis();
    lastSecMs = bootMs - 1000;
    aBlinkNext = bootMs + 1500;
    readClock();
}

void AppCodey::onRunning()
{
    if (_keys.update() == input::KeyEvent::GoHome || openWifiSetup) {
        if (!_closing) {
            _closing = true;
            stopWorkers();
        }
    }
    if (_closing) {
        if (factoryLinkStopped() && audioDone.load()) {
            close();
        }
        vTaskDelay(pdMS_TO_TICKS(5));
        return;
    }
    if (canvasReady) {
        factoryReceiveLink();
        codeyTick();
        if (!g_voice || g_vphase != 1) {
            audioSequence.store(-1);
        }
        factorySendControls();
    }
    vTaskDelay(pdMS_TO_TICKS(3));
}

void AppCodey::onClose()
{
    stopWorkers();
    while (!factoryLinkStopped() || !audioDone.load()) {
        GetHAL().feedTheDog();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
    factoryReleaseLink();
    if (audioFrames) {
        vQueueDelete(audioFrames);
        audioFrames = nullptr;
    }
    if (g_voiceSB) {
        vStreamBufferDelete(g_voiceSB);
        g_voiceSB = nullptr;
    }
    for (auto* sprite : {&cv, &g_ringA, &g_ringB, &g_voiceBg}) {
        sprite->deleteSprite();
    }
    GetHAL().setBackLightBrightness(originalBrightness);
    GetHAL().setButtonConfig(originalButtonConfig);
    GetHAL().startLvglUpdate();

    if (openWifiSetup) {
        auto& manager = mooncake::GetMooncake();
        int settings = -1;
        int launcher = -1;
        for (const auto& app : manager.getAllAppProps()) {
            if (app.info.name == "Settings") settings = app.appID;
            if (app.info.name == "Launcher") launcher = app.appID;
        }
        if (settings >= 0 && launcher >= 0) {
            auto* instance = manager.getAppAbilityManager()->getAbilityInstance(launcher);
            if (instance) {
                static_cast<mooncake::templates::AppLauncherBase*>(instance)->openApp(settings);
            }
        }
    }
}
