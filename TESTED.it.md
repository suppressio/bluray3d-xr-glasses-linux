[🇬🇧 English](TESTED.md) | 🇮🇹 Italiano

# Provato: dispositivi, player, dischi

Con cosa è stato provato finora questo progetto, e come è andata. Il [README](README.it.md) spiega i passi generali; qui ci sono i dettagli per ogni dispositivo e player, e i dischi che hanno insegnato qualcosa. Altre segnalazioni sono benvenute: [It works with my device or player](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/new/choose).

- [Dispositivi e player](#dispositivi-e-player)
  - [VITURE Pro XR + Pro Neckband: 3D Player ufficiale](#viture-pro-xr--pro-neckband-3d-player-ufficiale)
  - [PICO 4: File Manager e lettore video di sistema](#pico-4-file-manager-e-lettore-video-di-sistema)
  - [Altri player](#altri-player)
- [Dischi](#dischi)
- [Sul PC](#sul-pc)

---

## Dispositivi e player

### VITURE Pro XR + Pro Neckband: 3D Player ufficiale

✅ **Funziona, 3D compreso.** Il player per cui il progetto è nato.

- [ ] Apri il **3D Player**, vai nella scheda **Rete locale** e aggiungi il PC: il suo indirizzo IP (sul PC lo trovi con `hostname -I`), accesso ospite / anonimo.
- [ ] Apri **Disks**, poi la cartella e il film (se una cartella sembra vuota, torna indietro e rientra).
- [ ] Il player riconosce il formato affiancato e passa in 3D da solo. 🎉

![La riproduzione nel 3D Player VITURE: 3D attivo, barra di avanzamento, nome del file](docs/viture-3d-playback.png)

Da sapere:
- **Non ha un menu delle tracce audio** e ne sceglie una da solo: per questo di base ogni lingua è un file a sé ([Lingue audio](OPTIONS.it.md#lingue-audio)).
- Carica i **file di sottotitoli esterni** accanto al video e li disegna correttamente in entrambi gli occhi.
- Taglia i nomi lunghi dei file: per questo la lingua sta all'inizio del nome.
- Apre solo file da una cartella SMB, non flussi di rete.
- **Non apre proprio un rip MKV di un Blu-ray 3D** (MVC): non parte. Qui il file SBS virtuale serve davvero.
- Chiudere il programma mentre un film è in riproduzione blocca il player per circa 20 secondi (vedi [Risoluzione dei problemi](README.it.md#risoluzione-dei-problemi)).

### PICO 4: File Manager e lettore video di sistema

✅ **Funziona, 3D compreso, senza app aggiuntive.** Il File Manager del visore apre le cartelle di rete, e il suo lettore video riproduce il 3D affiancato in un cinema virtuale.

- [ ] Apri **File Manager** dalla barra in basso, poi **Local Network** a sinistra. Il PC compare da solo (col nome e con l'indirizzo IP).
- [ ] La prima volta un avviso **Security Risk** dice che la connessione non è cifrata: **Got it** (è la rete di casa, e la cartella è in sola lettura).
- [ ] Apri **Disks**, poi la cartella e il film.
- [ ] Se l'immagine mostra due figure affiancate, premi il pulsante a cubo nella barra del player (**Switch Mode**): **Non-VR** e **3D SBS**.
- [ ] Nel menu a ingranaggio (**Advanced Settings → Aspect Ratio**) scegli **16:9**. 16:10 riempie di più lo schermo, ma allunga un po' l'immagine in altezza. 🎉

![Il pannello Switch Mode del lettore del PICO 4: Non-VR, 3D SBS](docs/pico-switch-mode.png)

![Il File Manager del PICO 4 nella cartella Blu-ray 3D: un file per lingua, con e senza sottotitoli](docs/pico-file-manager.png)

![Un Blu-ray 3D nel lettore video del PICO 4, nel cinema virtuale](docs/pico-cinema.png)

Da sapere:
- Il menu della scena (icona della montagna) sceglie la sala (per esempio **PMAX Cinema**), dove ti siedi (**Close / Centre / Far**) e la dimensione dello schermo.

### Altri player

| Dispositivo | Player | Risultato |
|---|---|---|
| Visore PICO | **VLC** | ✅ I Blu-ray 2D vanno (provato con Ready Player One). Ha il menu delle tracce audio, quindi qui tornano comodi i file di `Multi-audio/`. |
| Neckband VITURE | **VLC** | ⚠️ Riproduce, ma bisogna uscire dall'interfaccia SpaceWalker e passare alla modalità Android, avviare il video e _solo dopo_ mettere gli occhiali in 3D. Scomodo, e a volte gli occhiali sono rimasti bloccati in modalità 3D (ho dovuto staccare il cavo). |
| Neckband VITURE | **XPlayer2** | ❌ Sul mio Neckband non ha funzionato, con nessun video. |

Con VLC su un Wi-Fi debole aiuta alzare la sua cache di rete ([Rete e qualità](OPTIONS.it.md#rete-e-qualità)).

---

## Dischi

| Disco | Tipo | Note |
|---|---|---|
| **Tron: Legacy** | Blu-ray 3D | Il disco di riferimento delle prove. Il film è fatto di due clip, riprodotte in sequenza. AACS, decifrato con tutti e tre i metodi. |
| **Mad Max: Fury Road** | Blu-ray 3D | Tre lingue audio e sottotitoli; visto in 3D sul PICO 4. |
| **Ready Player One** | Blu-ray 2D | Sottotitoli dal disco; riprodotto anche in VLC su un PICO. |
| **Cowboy Bebop** (disco 1) | Blu-ray 2D | 5 episodi in un unico titolo "riproduci tutto" da 120 minuti: esce come un solo file. |
| **Ritorno al futuro** | DVD (PAL) | Sugli occhiali come un file locale, sottotitoli OK. |
| **Utopia**, stagione 1 disco 1 | DVD (PAL) | Un disco di una serie inglese con una protezione a decine di titoli finti, e un punto rovinato: gli episodi escono giusti, il punto rovinato viene saltato. |

D'ora in poi si aggiungono solo i dischi con qualcosa di particolare: un problema, una struttura insolita, una protezione. Non ancora provati: dischi con **BD+** (dovrebbero aprirsi tramite MakeMKV), dischi con più angolazioni, DVD **NTSC**.

---

## Sul PC

- **Linux**: Debian testing (installazione nativa); lo script di installazione verificato anche in container puliti di Debian 13 e Ubuntu 24.04 (FFmpeg 7.1 e 6.1), e l'immagine Docker.
- **Decifratura**: libaacs con un `KEYDB.cfg`, e MakeMKV 2.0.0 (libmmbd); cartelle BDMV decifrate.
- **Codifica**: NVIDIA NVENC e x264 sulla CPU.
- **Lettore**: un lettore Blu-ray SATA interno; espulsione e inserimento mentre il programma gira.
