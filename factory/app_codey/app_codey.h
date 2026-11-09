#pragma once

#include <apps/common/key_manager/key_manager.h>
#include <mooncake.h>

class AppCodey : public mooncake::AppAbility {
public:
    AppCodey();
    void onOpen() override;
    void onRunning() override;
    void onClose() override;

private:
    input::KeyManager _keys;
    bool _closing = false;
};
