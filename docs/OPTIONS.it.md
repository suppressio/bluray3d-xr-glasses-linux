🌐 [English](OPTIONS.md) | **Italiano**

# Le opzioni nel dettaglio

> Riferimento, aggiornato: tutte le opzioni del programma, per la strada nativa e per Docker.

Le impostazioni predefinite funzionano: questa pagina serve solo se vuoi cambiare cosa offre la cartella condivisa (lingue, sottotitoli, qualità) o come si comporta. Torna al [README](../README.it.md).

**Dove si mettono le opzioni:**
- strada nativa: sulla riga di comando, per esempio `bluray3d-xr --audio-lang ita,eng --subs ita,eng /dev/sr0`;
- Docker: nel file `.env` (vedi `.env.example`), poi `docker compose up -d`.

| Opzione | `.env` (Docker) | Riga di comando (nativo) | Predefinito |
|---|---|---|---|
| Lettore Blu-ray | `DRIVE` | argomento, per esempio `/dev/sr0` | — |
| Cartella con ISO / BDMV / MKV | `MOVIES_DIR` | argomenti posizionali | — |
| [Lingue audio](#lingue-audio) | `AUDIO_LANG` | `--audio-lang ita,eng` oppure `all` | `all` |
| [Un file per lingua, uno con tutte, o entrambi](#lingue-audio) | `AUDIO_FILES` | `--audio-files per-language\|single\|both` | `per-language` |
| [Lingue dei sottotitoli](#sottotitoli) | `SUBS` | `--subs ita,eng`, `all` oppure `none` | `all` |
| [Profondità dei sottotitoli 3D](#sottotitoli) (pixel) | `SUB_DEPTH` | `--sub-depth 8` | `8` |
| [Copie a bitrate ridotto in `Light/`](#rete-e-qualità) | `LIGHT=on\|off` | `--light` / `--no-light` | attive |
| [Animazione dopo un salto](#animazione-di-caricamento-dopo-un-salto) | `LOADER` | `--loader retrowave\|simple\|none\|video.mp4`, `--loader-3d video.mp4` | `retrowave` |
| Encoder video | `ENCODER` | `--encoder auto\|nvenc\|x264` | `auto` (NVENC se c'è) |
| [Condivisione via Samba, DLNA, o entrambe](#dlnaupnp-invece-di-samba) | `SHARE` | `--share samba\|dlna\|both` | `samba` |
| [Porta DLNA/HTTP](#dlnaupnp-invece-di-samba) | `HTTP_PORT` | `--http-port` | `8201` |
| Punto di montaggio | — | `--mount` | `/srv/bd3d` |
| [File di log del programma](#log) | — | `--log percorso` o `none` | `~/.local/state/bluray3d-xr/bluray3d-xr.log` |
| [Log della pipeline](#log) | — | `--log-file` | `/tmp/bd3d-pipeline.log` |
| [Log dei salti](#log) | — | `--debug` | spento |

---

## Sorgenti

Oltre al lettore, il programma accetta lo stesso contenuto in altre forme (anche tutte insieme):

| Sorgente | Esempio | Note |
|---|---|---|
| Lettore Blu-ray | `/dev/sr0` | il film compare quando inserisci un disco e sparisce quando lo togli |
| Immagine ISO | `~/Video/3D/Tron.iso` | letta e decifrata come il disco |
| Cartella BDMV | `~/Video/3D/Tron/` (contiene `BDMV/`) | per esempio un "backup" di MakeMKV; quelle non cifrate non richiedono chiavi |
| Cartella VIDEO_TS | `~/Video/DVD/RAF/` (contiene `VIDEO_TS/`) | un DVD copiato su disco; i file ISO possono essere Blu-ray o DVD |
| Rip MKV | `~/Video/3D/Tron.mkv` | un rip di MakeMKV che ha conservato il 3D (MVC); i file senza 3D vengono saltati |
| Cartella | `~/Video/3D` | esplorata con tutte le sottocartelle, per tutto quanto sopra |

```console
bluray3d-xr --audio-lang ita,eng /dev/sr0 ~/Video/3D
```

Le cartelle vengono lette all'avvio: per vedere un file nuovo riavvia il programma. I lettori invece sono controllati di continuo.

## Serie TV

Un Blu-ray con più episodi (un titolo per episodio, nell'ordine del "play all": vedi [RESEARCH.it.md](RESEARCH.it.md)) ha ogni episodio nominato `S<stagione>E<episodio> - <Serie>.ts`, per esempio `S01E05 - Rick and Morty.ts`; la parte stagione/episodio viene sempre subito dopo il prefisso di lingua/sottotitoli (`ITA - `, `subITA - `...), perché i lettori tagliano i nomi lunghi nell'elenco dei file.

La stagione si legge dal nome del disco o dall'etichetta del volume (`Staffel 1`, `Season 1`, `Stagione 1`, `Saison 1`, `S1`...), senza alcuna ricerca in rete. Il numero del primo episodio non è scritto sul disco, quindi viene solo ipotizzato (1, 2, 3...) quando né il nome del disco né l'etichetta lasciano intendere che contenga solo una parte della stagione (`Disco 2`, `Disc 2`, `Vol. 2`...); una stagione divisa su più dischi, o un disco senza una stagione riconosciuta, mantiene invece la nomenclatura di oggi (`<Serie> - <episodio>`).

Un disco 3D non è mai una serie quando un titolo "play all" in 3D copre tutte le parti: è un film diviso in parti (Tron: Legacy 3D ha anche le due metà come titoli a sé), e si carica come un solo film.

I contenuti extra di un disco (making-of, scene eliminate...) vanno in una sottocartella `Extra/`, numerati separatamente dagli episodi.

## DLNA/UPnP invece di Samba

Per default i file sono montati solo per Samba, la cartella `[Disks]` (`--share samba`). `--share dlna` annuncia gli stessi file via DLNA/UPnP invece che via SMB, per i player che esplorano un media server invece di aprire una cartella di rete (smart TV, Kodi, Moon VR…); in questa modalità non servono né FUSE né Samba. `--share both` fa entrambe le cose insieme, per esempio per servire una TV via DLNA e un telefono via SMB dallo stesso programma.

```console
bluray3d-xr --share dlna --audio-lang ita,eng /dev/sr0
```

- Il server ascolta su `--http-port` (predefinito `8201`). Apri `http://<ip-pc>:8201/` in un browser per vedere le stesse cartelle come una pagina semplice: un modo rapido per controllare che funzioni, e qualsiasi player che apre un video da un URL può usarlo direttamente.
- Sul player, cerca "bluray3d-xr" tra i media server DLNA che trova in rete (nessun indirizzo IP da digitare): entra in `Blu-ray 3D/`, `Blu-ray/`, `DVD/`, le stesse cartelle della condivisione SMB.
- Il salto funziona come su SMB: un byte del file corrisponde sempre a un secondo preciso del film, quindi un salto riavvia la pipeline da lì, che il player legga via SMB o via HTTP.
- Docker, solo DLNA: `docker compose -f docker-compose.yml -f docker-compose.dlna.yml up -d --build` — niente Samba, niente FUSE, niente porta 445; usa la rete dell'host, perché la scoperta SSDP è multicast (la porta si imposta con `HTTP_PORT` in `.env`). DLNA insieme a Samba: `docker compose -f docker-compose.yml -f docker-compose.both.yml up -d --build`, anche questo sulla rete dell'host, quindi Samba usa direttamente la porta 445 dell'host (`SMB_PORT` non vale).
- Installazione nativa: `scripts/install.sh --share dlna` salta del tutto i pacchetti Samba/pyfuse3/fuse3, il mount FUSE e `smb.conf`. `scripts/install.sh --share both` installa tutto; rilanciarlo con `both` dopo un'installazione `dlna` aggiunge le parti Samba/FUSE che aveva saltato. `scripts/update.sh` mantiene il tipo scelto all'installazione.

## Lingue audio

Per default vengono offerte tutte le lingue del disco, una traccia ciascuna (la migliore: DTS-HD MA, DTS, AC-3… prima della TrueHD). `--audio-lang ita,eng` limita la scelta e ne fissa l'ordine.

Le lingue sono i codici standard ISO 639: `ita`, `eng`, `fra`, `deu`, `spa`, `jpn`… Vanno bene anche le due grafie delle lingue che ne hanno due (`fra`/`fre`, `deu`/`ger`, `nld`/`dut`…) e i codici a due lettere (`it`, `en`), sia per `--audio-lang` sia per `--subs`.

Ogni lingua diventa **un file a sé** (`ITA - film - 3D SBS.ts`, `ENG - film - 3D SBS.ts`; la lingua è all'inizio perché i player tagliano i nomi lunghi). Il 3D Player VITURE non ha un menu per le tracce audio e ne sceglie una da solo, per questo è il comportamento predefinito.

Se il tuo player il menu ce l'ha (VLC sì), `--audio-files single` mette tutte le lingue in un file solo. `both` offre le due cose insieme: i file per lingua, più quello con tutte le lingue in una cartella `Multi-audio/`. È utile con più dispositivi; i file sono virtuali, quindi quelli in più non costano nulla.

## Sottotitoli

I sottotitoli del disco (Blu-ray, Blu-ray 3D, DVD, MKV 3D) vengono **disegnati nell'immagine**, come fa un lettore da salotto: niente file esterni, quindi funzionano con qualsiasi player.

- Ogni lingua dei sottotitoli è una versione in più di ogni file audio: `ITAsubITA - film`, `ITAsubENG - film` (audio italiano con sottotitoli italiani / inglesi); in `Multi-audio/` si chiamano `subITA - film`.
- Il semplice `ITA - film` è senza sottotitoli, tranne quelli **forzati** della sua lingua (le battute in lingua straniera), che vengono sempre disegnati.
- I dischi hanno spesso più di 10 lingue di sottotitoli e il predefinito `all` le offre tutte, per ogni lingua audio: **imposta `--subs` con quelle che leggi**, per esempio `--subs ita,eng` (`none` per nessuna versione sottotitolata).
- **I sottotitoli 3D** sono disegnati in entrambi gli occhi, ogni copia spostata verso l'interno di `--sub-depth` pixel (predefinito 8), così galleggiano poco davanti allo schermo. Aumentalo se sembrano "dentro" la scena; `0` li mette sul piano dello schermo.
- Funzionano anche **file di sottotitoli esterni**: mettili accanto a un ISO/BDMV/MKV con lo stesso nome (`film.srt`, `film.ita.srt`, anche `.ass`, `.sup`…). Compaiono accanto a ogni video virtuale con il nome abbinato, e il player li carica come sottotitoli esterni (il 3D Player VITURE li mostra correttamente in entrambi gli occhi).

## Rete e qualità

Ogni file ha un **bitrate costante**: è quello che permette di far corrispondere un byte del file a un secondo del film. Quindi un file **non può adattarsi alla rete** come fa YouTube. Ogni film viene invece offerto a due bitrate:

| | Normale | `Light/` |
|---|---|---|
| **3D** (Full-SBS 3840×1080) | 24 Mbit/s (video 20) | 10 Mbit/s (video 8) |
| **2D** (1920×1080) | 15 Mbit/s (video 12) | 6,5 Mbit/s (video 5) |
| **DVD** (1024×576 PAL, 854×480 NTSC) | 5 Mbit/s (video 4) | 2,4 Mbit/s (video 1,8) |

Audio: AAC stereo 192 kbit/s per lingua (DTS e TrueHD non sono supportati dalla maggior parte dei player mobili); un file con più lingue (`Multi-audio/`) cresce di 0,22 Mbit/s per ogni lingua in più. Video: H.264. `--no-light` toglie le copie in `Light/`.

Se la riproduzione **si ferma ogni pochi secondi**, il Wi-Fi non regge quel bitrate (basta un muro spesso):
- apri lo stesso film da **`Light/`**;
- in **VLC** aumenta la cache di rete (*Impostazioni → Avanzate → Cache di rete*) a 5000-10000 ms: assorbe i brevi cali del Wi-Fi;
- il programma se ne accorge e lo scrive nel suo log, dicendo anche se il film era pronto (allora è lenta la rete o il player) o no (allora è lento il disco o la decodifica):
  ```
  Blu-ray/ITA - Ready Player One.ts: the player receives 77% of the data rate the movie needs,
  with 60s of movie ready ahead: the network (or the player) is too slow for this file,
  playback will pause (try Light/)
  ```
  Se invece dice "only 0.5s of movie is ready ahead: the disc or the decoding cannot keep up", `Light/` non aiuta: guarda il disco (graffi, impronte) e la CPU.

## Animazione di caricamento dopo un salto

Dopo un salto con la barra il film ha bisogno di qualche secondo (il lettore si sposta, poi parte la decodifica). Nel frattempo il player mostra l'ultima immagine, ferma, e il suo contatore non avanza: sembra bloccato, ma poi il film riparte **esattamente dove hai saltato**.

Per questo il file, di default, contiene un'animazione di caricamento, in loop, dal punto di arrivo finché il film non è pronto: una griglia al neon che corre verso l'orizzonte, il disco che sorge come il sole, una barra di caricamento che salta a ritmo. Nei film 3D è in 3D anche lei.
- vedi subito che sta caricando;
- però durante l'animazione il contatore del player continua ad avanzare, e il film riparte **altrettanti secondi dopo** il punto in cui hai saltato: meno di un secondo su un DVD, 2-5 s su un Blu-ray dal lettore. Le due cose non possono stare insieme: ogni secondo del file è un secondo del film, e i secondi occupati dall'animazione non si possono riusare.

Non compare quando il player è in pausa (aspetta il fotogramma del film) né alla prima apertura di un file.

- `--loader simple` (Docker: `LOADER=simple`): una più sobria, un disco e un arco che gira, in 2D.
- `--loader none` (Docker: `LOADER=none`): niente animazione. Il player aspetta sull'immagine ferma e il film riparte esattamente dove hai saltato.
- `--loader video.mp4`: un'animazione tua, qualsiasi video breve che si ripete senza scatti. In 16:9: nei film 3D compare in entrambi gli occhi. Affiancato (3840x1080): in 3D nei film 3D, il suo occhio sinistro in quelli 2D.
- `--loader-3d video.mp4`: un altro video affiancato, solo per i film 3D.

Le animazioni incluse (`loaders/retrowave.mp4`, 1 MB, affiancata; `loaders/simple.mp4`, 100 KB) sono fatte da zero in Blender: niente immagini o font di altri. Gli script che le generano sono in [bluray3d-xr-loaders](https://github.com/suppressio/bluray3d-xr-loaders).

## Log

- Il log del programma dice quali film compaiono, da dove parte ogni pipeline e se la rete e il disco reggono. Va nel terminale ed è anche salvato, con la data, in `~/.local/state/bluray3d-xr/bluray3d-xr.log` (fino a 5 MB, poi tre copie vecchie `.1` `.2` `.3`); `--log` lo salva altrove, `--log none` per niente. Con Docker: `docker compose logs`.
- `--log-file` è l'output di FFmpeg e dei decoder, una sezione per pipeline (oltre i 10 MB la parte vecchia passa in `.1`): il primo posto dove guardare se l'immagine è sbagliata.
- `--debug` registra anche ogni salto e le letture del player nei 20 secondi successivi: quanto ha aspettato e se sembrava in pausa. Anche il comando completo di ogni pipeline, per rilanciarlo a mano ([DEVELOPMENT.it.md](DEVELOPMENT.it.md#smontare-una-pipeline)).
