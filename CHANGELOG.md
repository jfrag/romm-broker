# Changelog

## [0.3.0](https://github.com/romm-streaming/romm-broker/compare/v0.2.0...v0.3.0) (2026-08-17)


### Features

* add PPSSPP emulator module with working save/load-state ([abae001](https://github.com/romm-streaming/romm-broker/commit/abae001fe7375ff3013605a190f6b540f4728ffc))


### Bug Fixes

* add PPSSPP emulator module with working save/load-state ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))
* Merge pull request [#13](https://github.com/romm-streaming/romm-broker/issues/13) from romm-streaming/dev ([0e8c013](https://github.com/romm-streaming/romm-broker/commit/0e8c0131cbccc42b01121c5e763e91b970379fe3))

## [0.2.0](https://github.com/romm-streaming/romm-broker/compare/v0.1.0...v0.2.0) (2026-08-17)


### Features

* add boot_failed field to the Emulator base class ([cfc3ab8](https://github.com/romm-streaming/romm-broker/commit/cfc3ab8232d5b1857545e0895a0a576d53cc399b))
* add disc-swap contract to the emulator base class ([61d3c4e](https://github.com/romm-streaming/romm-broker/commit/61d3c4effb3dcd6e94eb5cc11b643b9578f91d2b))
* gate the room comms surface on the session multiplayer flag and add invite links ([9934ccd](https://github.com/romm-streaming/romm-broker/commit/9934ccdee8787408675eb67fe14947e9e6b26cf5))
* generalize PCSX2's deferred-load thread into a boot watchdog ([b556428](https://github.com/romm-streaming/romm-broker/commit/b5564284d0d9aa89e3d49ae4e52694fcc5d61b59))
* prefer m3u playlists on retroarch disc platforms ([2c8dcd9](https://github.com/romm-streaming/romm-broker/commit/2c8dcd9eab25bb42127e57501e48ec5b519a90b1))
* **retroarch:** link core assets so the ppsspp core can boot ([976b1f3](https://github.com/romm-streaming/romm-broker/commit/976b1f3f4c0dde5077dc33bc96ab03d7d83d0bd1))
* **room:** move track capture/presentation onto a worker-based pipeline ([3281f6e](https://github.com/romm-streaming/romm-broker/commit/3281f6eb0572dd42c16cdc10ca2820f649d247e4))
* serve swap-disc on the webstation broker ([f9db17e](https://github.com/romm-streaming/romm-broker/commit/f9db17ea78e2d120caa97e85d3dcf73c45a20dca))
* surface PCSX2 boot-failure detection on GET /api/session/status ([bd109ab](https://github.com/romm-streaming/romm-broker/commit/bd109ab119145b5d9e46caca951dd33691711f6c))
* swap discs on a running retroarch core ([85e8f5c](https://github.com/romm-streaming/romm-broker/commit/85e8f5c71e00278917a227e09e9352fb6391966d))
* track the retroarch playlist and mounted disc index ([1621d41](https://github.com/romm-streaming/romm-broker/commit/1621d41a9de4d18c08294d97d108ff8273c178fd))
* **xemu:** add XEMU_SOFTWARE_GL to force CPU rendering for xemu alone ([658d0b4](https://github.com/romm-streaming/romm-broker/commit/658d0b46b57b1e523336b8c0a4a964104a524ee0))
* **xemu:** pin fullscreen on startup alongside the renderer ([b9565e6](https://github.com/romm-streaming/romm-broker/commit/b9565e639ea0baa7a9d6bcee27ac18619d0ff678))


### Bug Fixes

* guard against a dead or superseded core committing a disc swap ([bb13df3](https://github.com/romm-streaming/romm-broker/commit/bb13df3969c93a1e05528828f8bed1db0bdef7ca))
* lock disc swaps against each other and the deferred resume load ([f8022ad](https://github.com/romm-streaming/romm-broker/commit/f8022adc08d5c125e5451483dde5549fa2bb7480))
* match xemu save directories on the disk's own case ([1caf2c8](https://github.com/romm-streaming/romm-broker/commit/1caf2c87bc6354f474cb148bd101e31af1915198))
* pin the retroarch joypad driver to linuxraw so selkies pads register ([37ad1be](https://github.com/romm-streaming/romm-broker/commit/37ad1beedb6d7f83c515779c5d38074615c86feb))
* reap orphaned emulators on broker start and let an exit skip the state save ([5244955](https://github.com/romm-streaming/romm-broker/commit/52449552d1ae9289c7438cd1fdc94bd12e91347f))
* **retroarch:** drop the inline platform table shadowing the json one ([8160d6f](https://github.com/romm-streaming/romm-broker/commit/8160d6fa7bacf6a5b808dc76b8be73092f758578))
* **retroarch:** link ppsspp assets where the core actually reads them ([16ee9e0](https://github.com/romm-streaming/romm-broker/commit/16ee9e00d6551cc27bfec723c20f146d3750fca6))
* run the startup reap on the app that is actually served ([c30e2ca](https://github.com/romm-streaming/romm-broker/commit/c30e2ca603db12ff111d63ad39e41f01f22ec6ee))
* treat resume slot 0 as a resume request, not as no request ([b8536c0](https://github.com/romm-streaming/romm-broker/commit/b8536c0fa2bd6b65d9206b85639668ee9760eea9))
* **xemu:** pin the renderer to OpenGL before each launch ([c1f6f73](https://github.com/romm-streaming/romm-broker/commit/c1f6f732c740cbdd0a06dad400a8f5c1c57a4fba))


### Documentation

* trim unsupported emulator references from the readme ([13c7aea](https://github.com/romm-streaming/romm-broker/commit/13c7aead50a6ccea9325e45fb5f6cd3e447bd0d8))

## 0.1.0 (2026-08-08)


### Features

* add standalone dolphin launcher to the webstation broker ([7949385](https://github.com/romm-streaming/romm-broker/commit/794938522d5205c6844663e53dc18775de82d7e1))
* sync save states between the webstation broker and RomM ([3663ec4](https://github.com/romm-streaming/romm-broker/commit/3663ec42be9ede35fe23aafc751dbbbea6436ae5))
* sync the whole PS2 memory card as a folder card ([12fcd8c](https://github.com/romm-streaming/romm-broker/commit/12fcd8c66c0dab78eec3a79f950fc34504aef0a0))


### Bug Fixes

* disable savestate thumbnails for GPU-rendered dolphin core ([584fc7f](https://github.com/romm-streaming/romm-broker/commit/584fc7feadf14d81c5f07601bc293380d510291a))
* keep the exit state readable after the session is torn down ([6465922](https://github.com/romm-streaming/romm-broker/commit/64659220d83bdfc60d3fa8c5660096f6b868d21a))
* lay down the pcsx2 folder card marker so the slot 1 card is recognized ([c700f96](https://github.com/romm-streaming/romm-broker/commit/c700f96db66bb8ce7034613b47c97c60e7a61aa1))
* pin dolphin's gamecube slot a to the gci folder card device ([7bda8c5](https://github.com/romm-streaming/romm-broker/commit/7bda8c5836b5addf77eace4bccd27576a7699a4c))
* skip a synced memory card left in an older save archive instead of failing the restore ([97cc401](https://github.com/romm-streaming/romm-broker/commit/97cc4019183bca8510d3867922e905c3547e82bf))


### Documentation

* add reverse proxy guide for serving the container from the parent origin ([653bbcf](https://github.com/romm-streaming/romm-broker/commit/653bbcf070631f16bbfe47c02a9a747de1a8346c))
* document the state routes and the retroarch launcher ([cea0c29](https://github.com/romm-streaming/romm-broker/commit/cea0c296d055ca7220d1dfc2afe84f81e15f26a9))
* replace the Zoraxy virtual directory recipe with a host rule ([9e22c68](https://github.com/romm-streaming/romm-broker/commit/9e22c68d95d234de06c4507fa8124ddb90e6605a))
