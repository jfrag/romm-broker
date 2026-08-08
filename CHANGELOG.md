# Changelog

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
