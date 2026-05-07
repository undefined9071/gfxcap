// stub.cpp -- minimal no-op DLL used as a diagnostic baseline.
//
// This DLL does NOTHING: DllMain returns TRUE without any initialization,
// no hook installation, no thread spawn, no named pipes, no mutexes.
//
// Purpose: when investigating environment-specific failures (e.g. a host
// process that terminates shortly after gfxcap.dll loads), build this
// stub via .gfxcap/scripts/build_stub.py and substitute it for gfxcap.dll
// at the inject point. Comparing behaviour between the stub and the real
// DLL helps separate detections caused by the hook engine activity from
// detections caused by simpler signals (presence of the DLL on disk, the
// loader chain, PE characteristics, etc.).

#include <windows.h>

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    if (reason == DLL_PROCESS_ATTACH)
        DisableThreadLibraryCalls(hinst);
    return TRUE;
}
