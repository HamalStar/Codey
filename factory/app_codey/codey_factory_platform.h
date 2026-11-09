#pragma once

#include <hal/hal.h>
#include <esp_random.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>

using std::max;
using std::min;
static constexpr float PI = 3.14159265358979323846f;
static constexpr float DEG_TO_RAD = PI / 180.0f;

#define CODEY_BTN_A GetHAL().btnA
#define CODEY_BTN_B GetHAL().btnB

static uint32_t millis() { return GetHAL().millis(); }
static void delay(uint32_t ms) { GetHAL().delay(ms); }
static long random(long upper) { return esp_random() % upper; }

template<class T, class L, class H>
static T constrain(T value, L low, H high)
{
    return std::clamp(value, static_cast<T>(low), static_cast<T>(high));
}

// The stock console remains on UART; only protocol bytes go to USB.
static struct {
    template<class... Args>
    void printf(const char* format, Args... args) const { std::printf(format, args...); }
    void println(const char* text) const { std::puts(text); }
} Serial;

static void factoryOpenWifiSetup();
static void factoryPlayChime(bool claude, uint8_t volume);
static void factorySavePrefs(uint8_t brightness, uint8_t volume);
static void factoryBeginAudio(uint16_t sequence);
static bool factoryReadAudio(uint16_t sequence, int16_t* pcm);
static void factorySetCompanionTime(long epoch);
static long factoryEpoch();
static void factoryUsbWrite(const uint8_t* head, const uint8_t* payload, uint16_t size, const uint8_t* tail);
static void factoryUsbRead();
