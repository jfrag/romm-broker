# Standalone emulator setup

Single game launches reuse whatever configuration the emulators already have, so
the workflow is: launch the container into desktop mode from the RomM web
interface, open each emulator you plan to use from the desktop, run its setup
wizard, configure your controller, install any system files or firmware it
needs, and play an actual title to confirm everything works. While you are in
there, tweak graphics and audio settings to your desired profile. All of this is
stored in the container's home directory and carries over to individual game
launching.

## Getting files into the container

Firmware blobs, decryption keys, BIOS images, and system modules can be dragged
and dropped straight into the browser window. This upload path is for system
files, not ROMs. Games come from your RomM library. Once uploaded, use the
pcmanfm file manager or a foot terminal on the desktop to move files into the
directories each emulator expects.

## Controllers

Controllers behave identically in the desktop view and in single game
launching, so a controller that works in a real game on the desktop will work
the same way when RomM launches that game directly. Always test a real game
with a real controller before calling an emulator done.

## Dolphin

Dolphin ships with default controller profiles and settings inside the
container environment on first launch. The only settings you should need to
touch are graphical preferences such as internal resolution and rendering
options.

> **Special note:** Dolphin may need to be opened and closed once before its
> titlebar decorations appear, so if the window looks undecorated on first
> launch, close it and reopen it to continue configuration.

## Eden

Eden needs prod keys and firmware installed before it is functional. Under
**Tools**, select **Install decryption keys** and point it at your uploaded
prod keys, then select **Install firmware** and point it at the folder or file
you uploaded.

Controller profiles are already set up for Eden on first container init. You
may also want to adjust graphics and audio settings, then run a game to
confirm.

## Xemu

Xemu needs three specific files, and by default it launches directly into the
settings menu that asks for them. Upload your files, then select the MCPX boot
ROM, the flash ROM (BIOS), and the hard disk drive image.

On first init the container extracts the hard disk drive to raw format and
backs up the qcow2 image in the same folder. This is needed for save
extraction and injection, so leave both files where they are.

Controller profiles are set up automatically on first init. Adjust graphics
and audio to your liking and test a game.

> **Special note:** Xemu must be relaunched after its default files are set up
> before it will boot anything.

## Xenia

Xenia (Xbox 360) runs through the Xenia Edge.
Controllers work out of the box through SDL, so the desktop visit is mostly
about the profile and graphics settings.

Xenia needs a profile before it can save anything, and it asks for one the
first time it starts with none: a **No Profiles Found** dialog offers to create
one. Launch Xenia once from the desktop, accept that prompt (or use the profile
menu's **Create new...**), enter a gamertag, and close Xenia. The new profile is
signed in automatically and Xenia remembers it in its config, so every later
launch signs into that profile silently and games see a logged-in Live profile
to save against.

Single game launches run with `--headless`, which auto-answers the guest
dialogs (storage device select, sign-in, message boxes) that nobody in the
stream could click through. The profile prompt is not one of those: it is
Xenia's own window, it appears even in headless mode, and a game launched
without a profile would sit behind it with no way to dismiss it.

Set your graphics options and test a real game from the desktop before
launching through RomM.

> **Special note:** Xenia must be launched once from the desktop to create a
> default Live profile for signing into games. Without it, single game launches
> stall on the profile prompt and games cannot save. Single game launches keep
> all Xenia data, the profile included, under `/config/xenia`; the profile has
> to be created against that same storage root to be the one games boot with.

## DuckStation

DuckStation runs a setup wizard on first launch. Upload your BIOS, select it
for the BIOS folder, and skip the game directory step. For controller setup,
make sure you pick the proper SDL devices: SDL 0 for player 1 and SDL 1 for
player 2.

Finish by setting the graphics options you want (renderer, internal
resolution, enhancements) and testing a game.

## shadPS4

shadPS4 needs everything below before it can launch games.

Launch shadps4qt and run the **version manager** in the top right. From that
menu select the version of shadPS4 you want to use; this same interface is how
you pull future updates. The RomM broker always uses the latest version you
have downloaded, preferring a pre-release if one is available. After a version
downloads, the container automatically extracts its AppImage so it can run in
a Docker environment.

Controllers are pre-configured for shadPS4.

Most games need sys_modules. Upload them and make sure they end up in
`/config/.local/share/shadPS4/sys_modules` (the directory is created
automatically the first time the program launches).

You may also want to point DLC content at your `/romm` directory in the
container; this depends entirely on how you have that folder organized.

As with the others, set graphics options to taste and confirm with a real
game.

> **Special note:** shadps4qt is only the desktop manager for shadPS4's core
> settings. You configure through it, but the binaries that single game
> launches actually use are the shadPS4 cores it downloads.

## PCSX2

PCSX2 runs a setup wizard much like DuckStation's. Upload your BIOS files and
select them, skip the game folder step, and for controller setup pick the
proper SDL devices: SDL 0 for player 1 and SDL 1 for player 2.

Set your graphics preferences and test a game before moving on.

## RetroArch

RetroArch is pre-configured, and cores are downloaded on demand for requested
launches. You may still want to adjust per-core or general settings from the
RetroArch main GUI on the desktop. The same menu is reachable during a single
game launch by pressing **F1**.

BIOS files and other RetroArch assets are too varied to cover here, so see the
[core-specific BIOS documentation](https://docs.libretro.com/library/bios/#links-to-the-core-specific-bios-information)
for what each core expects.

## RPCS3

RPCS3 needs its firmware installed from **File > Install Firmware**.
Controllers are pre-set up.

Adjust graphics settings as desired and run a game to confirm.

## Cemu

Cemu needs everything set up by hand: controller profiles, audio, and
decryption keys.

1. Launch Cemu once and close it so it creates its default directories.
2. Upload your `keys.txt` and place it in `/config/.local/share/Cemu`.
3. Launch Cemu and open controller settings. Select **Wii U Pro Controller**
   as the emulated controller; if that option is not available, set it up as a
   Wii U GamePad, save, exit, and reopen the settings.
4. Click the add button for the controller, select **SDL** as the driver type,
   and pick the first pad in the list of four. Confirm the controller works by
   moving the analog sticks and watching the live input display.
5. Under **General settings > Audio**, make sure TV is set to
   **Default Device**. You may need to close and re-enter this menu before the
   option appears, which lets Cemu detect the PulseAudio backend.

Finish with your preferred graphics settings and test a game.

> **Special note:** Cemu is finicky. Expect to launch it multiple times before
> all settings stick, and the controller profile menus are half broken: click
> the down arrows, click into the dropdowns, and navigate them with the
> keyboard to get them to behave.

## Azahar

Controllers are pre-configured for Azahar. Open it from the desktop, set
graphics and audio to your liking, and test a game to confirm.

Azahar stops rendering when the display resizes underneath it in fullscreen,
and the display resizes every time you resize your browser window. Single game
launches therefore start it windowed, which is not affected. Keep that in mind
on the desktop as well: if you put Azahar into fullscreen yourself and then
resize the browser, the picture freezes and you have to leave fullscreen to get
it back.

> **Special note:** For 3DS, the RetroArch core is the better option and is
> what a `3ds` launch uses by default. Azahar exposes no way for the broker to
> reach it from outside the process, so a standalone session has no save states
> and shuts down with a kill rather than a clean exit, meaning only what the
> game already wrote to its own save survives. The RetroArch core supports both
> save states and a proper exit. Use standalone Azahar when you want its own
> interface, and pick `retroarch` otherwise.
