[🇬🇧 English](README.md) | 🇮🇹 Italiano

# Blu-ray 3D sugli occhiali XR, da Linux
#### _Metti un Blu-ray 3D nel lettore del PC Linux e guardalo sugli occhiali XR (VITURE & co.) in 3D vero. Il PC legge, decifra e decodifica il disco al volo e lo serve in rete locale come video affiancato. Niente rip, niente conversione, niente spazio su disco._

***Nota:*** _Provato con VITURE Pro XR + VITURE Pro Neckband e il suo **3D Player** ufficiale. Qualsiasi player capace di aprire video da una cartella di rete (SMB) e di mostrare il 3D affiancato dovrebbe funzionare allo stesso modo. Per ora provato con un solo disco (Tron: Legacy 3D): vedi [Limiti](#limiti)._

---

## Il problema

Un Blu-ray 3D non contiene due video affiancati. Il 3D è registrato in **MVC** (H.264 _Multiview Video Coding_): un normale video 2D per l'occhio sinistro più una "vista dipendente" con le differenze per l'occhio destro.

- Gli occhiali XR, e quasi tutti i player 3D, vogliono l'**SBS**, _side-by-side_: i due occhi affiancati nello stesso fotogramma.
- In pratica **l'MVC non lo decodifica quasi nessuno**, a parte i lettori Blu-ray dedicati. Android e i suoi player non ci riescono, e FFmpeg scarta in silenzio la vista dipendente: il risultato è 2D.
- La soluzione solita è **copiare e convertire** ogni film in SBS: ore di lavoro e 10-20 GB per film.

## L'idea

Il PC legge il disco e decodifica l'MVC **mentre guardi**, e manda il risultato agli occhiali come se fosse un normalissimo file SBS.

```
Blu-ray 3D nel lettore (oppure un ISO / una cartella BDMV / un rip MKV)
        │  libbluray + libaacs: lettura e decifratura al volo
        ▼
   vista base + dipendente ─► edge264 (MVC → SBS 3840×1080) ─► encoder (NVENC o x264) + audio
        │
        ▼
   "ITA - film - 3D SBS.ts"  file virtuale in una cartella condivisa SMB (su disco non esiste)
        │   Wi-Fi
        ▼
   Occhiali: 3D Player ► Rete locale ► Disks ► film   → 3D automatico, pausa, seek
```

- **Inserisci il disco, compare il film.** Circa 15 secondi dopo la chiusura dello sportello il file compare nella cartella condivisa, con il nome del disco; togli il disco e sparisce.
- **Anche i Blu-ray normali (2D) e i DVD.** Stesso funzionamento: i Blu-ray 2D finiscono nella cartella `Blu-ray/`, i DVD in `DVD/`, i dischi 3D in `Blu-ray 3D/`.
- **Non si scrive niente su disco.** Il file `.ts` risulta di circa 22 GB ma non occupa spazio: ogni pezzo viene prodotto nel momento in cui il player lo legge.
- **Il seek funziona.** Il file ha bitrate costante, quindi ogni byte corrisponde a un secondo preciso del film. Quando il player salta, il PC riprende a leggere il disco da lì (qualche secondo: il lettore ottico deve riposizionarsi).
- **Gli occhiali vedono un file normale.** Niente app particolari né protocolli di streaming: interfaccia, riconoscimento del 3D, pausa e seek sono quelli del player.

Tutte le procedure descritte sono un compromesso ragionato tra il "manuale" e il guidato. È uno dei modi possibili per farlo.

## Requisiti

#### Hardware
- **Un lettore Blu-ray** nel PC Linux, che legge anche i DVD (provato: TSSTcorp SH-B123L).
- **Un PC Linux** sulla stessa rete degli occhiali. Decodificare l'MVC è lavoro per la CPU: provato su un Ryzen 9 5900X (decodifica a circa 9 volte il tempo reale). Non ho provato CPU meno potenti.
- **Facoltativa: una GPU NVIDIA**, per codificare con NVENC. Senza, il video viene codificato dalla CPU con x264 (sul 5900X comunque circa 6 volte il tempo reale).
- **Occhiali XR + un player** che apra video da una cartella di rete SMB e riproduca il 3D SBS. Provato: VITURE Pro XR + Pro Neckband, 3D Player ufficiale.
- **RAM**: circa 0,5 GB liberi mentre guardi un film. Su disco non si scrive nulla: la pipeline prepara in memoria fino a circa 256 MB in anticipo sul player e ne tiene circa 190 già letti per i piccoli salti indietro, più 16 MB (inizio e fine) per ogni file aperto.
- **Un buon Wi-Fi** (consigliati i 5 GHz): 15 Mbit/s per il 2D, 24 Mbit/s per il 3D, meno con le copie in `Light/` (vedi [Rete e qualità](#rete-e-qualità)).

#### Decifratura: le chiavi le porti tu
I Blu-ray commerciali sono cifrati (AACS, alcuni anche BD+). Il progetto **non fornisce e non scarica chiavi**; usa quello che hai, in quest'ordine:
1. **libaacs + `KEYDB.cfg`**: tutto open source. Metti un database di chiavi in `~/.config/aacs/KEYDB.cfg` (con Docker: nella cartella indicata come `AACS_DIR`).
2. **MakeMKV**, se installato e registrato: la sua libreria `libmmbd` decifra al posto di libaacs (anche il BD+).
3. Gli **ISO / le cartelle BDMV non cifrati** non richiedono chiavi.

I DVD (CSS) vengono decifrati da **libdvdcss**, che installi tu: su Debian/Ubuntu `sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg` (Debian: sezione `contrib`). Non è nell'immagine Docker; in `docker-compose.yml` c'è una riga commentata per usare quella del sistema.

> ⚖️ In molti paesi (Italia e gran parte dell'UE compresi) aggirare una protezione anticopia non è consentito, nemmeno per una copia privata. Verifica le regole del tuo paese.

#### Software sul PC
- **Docker** (strada A) _oppure_ un sistema **Debian/Ubuntu** (strada B). Tutto il resto lo installa il progetto:
  - [edge264-mvc](https://github.com/jens-duttke/edge264-mvc): l'unico decoder open source della vista dipendente MVC;
  - libbluray, libaacs, libbdplus (Blu-ray), libdvdread (DVD), FFmpeg, Samba (la cartella di rete), FUSE + pyfuse3 (il file virtuale).

---

## Passo 1 — Installazione: scegli la strada

| | **A. Docker** | **B. Script nativo** |
|---|---|---|
| Per chi | usa già Docker | Debian / Ubuntu |
| Tocca il sistema | no: Samba, FUSE e librerie stanno nel container | sì: pacchetti, `/opt`, `smb.conf`, `fuse.conf` (si toglie tutto con `uninstall.sh`) |
| Decifratura | libaacs + il tuo KEYDB (MakeMKV non è nell'immagine) | libaacs + KEYDB, oppure MakeMKV se installato |
| Codifica NVIDIA | serve il NVIDIA Container Toolkit | funziona subito |
| Samba già installato sul PC | conflitto sulla porta 445: va fermato | la condivisione si aggiunge alle tue |

Per prima cosa scarica il progetto:
```console
git clone https://github.com/suppressio/bluray3d-xr-glasses-linux.git
cd bluray3d-xr-glasses-linux
```

### Strada A — Docker

- [ ] Crea le impostazioni:
```console
cp .env.example .env
```
- [ ] Modifica `.env`:
  - `DRIVE=/dev/sr0` per tenere d'occhio il lettore Blu-ray; in `docker-compose.yml` togli il commento alla riga `- /dev/sr0` sotto `devices`;
  - `AACS_DIR` = la cartella che contiene il tuo `KEYDB.cfg`;
  - `MOVIES_DIR` = una cartella con ISO, cartelle BDMV o rip MKV (può essere vuota se usi solo il lettore);
  - `AUDIO_LANG` = le lingue che vuoi, per esempio `ita,eng`; vuoto o `all` = tutte le lingue del disco.
- [ ] Costruisci e avvia. La prima volta ci vuole qualche minuto, perché compila edge264 per la tua CPU:
```console
docker compose up -d --build
```
- [ ] Inserisci un Blu-ray 3D e guarda il log:
```console
docker compose logs -f
```
```
watching /dev/sr0: insert a 3D Blu-ray
/dev/sr0: disc inserted, opening it
+ ITA - Tron - Legacy 3D - 3D SBS.ts  (125 min, audio 0x1102 ita dts)
```

Per fermarlo: `docker compose down`.

##### GPU NVIDIA (facoltativo)
Installa il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), poi avvia con entrambi i file compose:
```console
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build
```
Nel log deve comparire `video encoder: nvenc`.
> ⚠️ Questa variante non l'ho ancora provata (sul mio PC non c'è il Container Toolkit). NVENC è provato con la strada nativa.

##### 💩 happens: porta 445 già occupata
Se sul PC gira già Samba, il container non può prendere la porta 445. I player di telefoni e occhiali di solito parlano solo con la 445. Quindi mentre guardi il film ferma il Samba del sistema (`sudo systemctl stop smbd`), oppure usa la strada B, che aggiunge una condivisione al Samba che hai già.

### Strada B — Script nativo (Debian / Ubuntu)

- [ ] Lancia l'installazione come utente normale (chiede `sudo` quando serve):
```console
./scripts/install.sh
```
Installa i pacchetti, compila edge264, installa il comando `bluray3d-xr` e aggiunge a Samba la condivisione `[Disks]`, in sola lettura e ad accesso ospite. L'intestazione di [`scripts/install.sh`](scripts/install.sh) elenca tutte le modifiche che fa.

- [ ] Se `ufw` è attivo, lo script stampa il comando per aprire la condivisione alla tua rete locale, per esempio:
```console
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```
- [ ] Il tuo utente deve poter leggere il lettore (su Debian/Ubuntu: il gruppo `cdrom`, di solito già assegnato agli utenti desktop).
- [ ] Avvialo quando vuoi guardare qualcosa:
```console
bluray3d-xr --audio-lang ita,eng --subs ita,eng /dev/sr0
```
Si ferma con `Ctrl+C`. Per togliere tutto: `./scripts/uninstall.sh`.

Lo script è provato con lettore e disco veri in container puliti Debian 13 (trixie) e Ubuntu 24.04; il programma gira sul mio PC con Debian testing.

---

## Passo 2 — Guardarlo sugli occhiali

##### Dal Neckband VITURE:
- [ ] Apri il **3D Player**, vai nella scheda **Rete locale** e aggiungi il PC: il suo indirizzo IP (sul PC lo trovi con `hostname -I`), accesso ospite / anonimo.
- [ ] Inserisci il disco nel PC e aspetta circa 15 secondi.
- [ ] Apri la cartella **Disks** (se sembra vuota, torna indietro e rientra). I dischi 3D sono in `Blu-ray 3D/` come `ITA - <film> - 3D SBS.ts`, quelli normali in `Blu-ray/` come `ITA - <film>.ts`: un file per ogni lingua audio, più uno per ogni lingua dei sottotitoli (`ITAsubENG - ...`: audio italiano, sottotitoli inglesi). Una cartella compare solo quando contiene un film. In `Light/` ci sono gli stessi film a un bitrate più basso (vedi [Rete e qualità](#rete-e-qualità)).

```
Disks/
├── Blu-ray 3D/
│   ├── ITA - Tron - Legacy 3D - 3D SBS.ts
│   ├── ITAsubENG - Tron - Legacy 3D - 3D SBS.ts
│   ├── ENG - Tron - Legacy 3D - 3D SBS.ts
│   └── Light/ ...
├── Blu-ray/
│   ├── ITA - Ready Player One.ts
│   └── Light/ ...
└── DVD/
    ├── ITA - Back To The Future.ts
    └── Light/ ...
```
- [ ] Aprilo. Il player riconosce il formato affiancato e passa in 3D da solo. 🎉

La prima apertura e ogni salto con la barra richiedono qualche secondo: è il lettore che si sposta nel nuovo punto.

**ATTENZIONE!** In alcuni film una parte delle scene è in 2D per scelta (in _Tron: Legacy_ le parti nel "mondo reale"). Lì i due occhi ricevono la stessa immagine: non è un errore.

##### Altri occhiali e altri player
Qualsiasi cosa apra video da una cartella SMB e mostri il 3D SBS dovrebbe andare. Qualche nota dalle mie prove sul Neckband:
- **VLC** lo riproduce, ma bisogna uscire dall'interfaccia SpaceWalker e passare alla modalità Android, avviare il video e _solo dopo_ mettere gli occhiali in modalità 3D. Funziona ma è scomodo, e a volte gli occhiali sono rimasti bloccati in modalità 3D (ho dovuto staccare il cavo).
- **XPlayer2** sul mio Neckband non ha funzionato, con nessun video.
- Il **3D Player non apre un rip MKV di un Blu-ray 3D**: non parte proprio.

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

## Opzioni

| Opzione | `.env` (Docker) | Riga di comando (nativo) | Predefinito |
|---|---|---|---|
| Lettore Blu-ray | `DRIVE` | argomento, per esempio `/dev/sr0` | — |
| Cartella con ISO / BDMV / MKV | `MOVIES_DIR` | argomenti posizionali | — |
| Lingue audio | `AUDIO_LANG` | `--audio-lang ita,eng` oppure `all` | `all`: tutte le lingue del disco |
| Un file per lingua, uno con tutte, o entrambi | `AUDIO_FILES` | `--audio-files per-language\|single\|both` | `per-language` |
| Lingue dei sottotitoli (versioni con i sottotitoli disegnati) | `SUBS` | `--subs ita,eng`, `all` oppure `none` | `all` |
| Profondità dei sottotitoli 3D (pixel) | `SUB_DEPTH` | `--sub-depth 8` | `8` |
| Copie a bitrate ridotto in `Light/` | `LIGHT=on\|off` | `--light` / `--no-light` | attive |
| Encoder video | `ENCODER` | `--encoder auto\|nvenc\|x264` | `auto` (NVENC se c'è) |
| Punto di montaggio | — | `--mount` | `/srv/bd3d` |
| Log della pipeline | — | `--log-file` | `/tmp/bd3d-pipeline.log` |

#### Lingue audio e sottotitoli
- **Lingue**: per default vengono offerte tutte le lingue del disco, una traccia ciascuna (la migliore: DTS-HD MA, DTS, AC-3… prima della TrueHD); `--audio-lang ita,eng` limita la scelta e ne fissa l'ordine. Ogni lingua diventa **un file a sé**
  (`ITA - film - 3D SBS.ts`, `ENG - film - 3D SBS.ts`; la lingua è all'inizio perché i player
  tagliano i nomi lunghi). Il 3D Player VITURE non ha un menu per le tracce audio e ne sceglie
  una da solo, per questo è il comportamento predefinito. Se il tuo player il menu ce l'ha,
  `--audio-files single` mette tutte le lingue in un file solo. `both` offre le due cose
  insieme: i file per lingua, più quello con tutte le lingue in una cartella `Multi-audio/`.
  È utile con più dispositivi; i file sono virtuali, quindi quelli in più non costano nulla.
- **I sottotitoli del disco** (Blu-ray, Blu-ray 3D, DVD, MKV 3D) vengono **disegnati
  nell'immagine**, come fa un lettore da salotto: niente file esterni, quindi funzionano con
  qualsiasi player. Ogni lingua dei sottotitoli è una versione in più di ogni file audio:
  `ITAsubITA - film`, `ITAsubENG - film` (audio italiano con sottotitoli italiani / inglesi);
  in `Multi-audio/` si chiamano `subITA - film`. Il semplice `ITA - film` è senza
  sottotitoli, tranne quelli **forzati** della sua lingua (le battute in lingua straniera),
  che vengono sempre disegnati.
  I dischi hanno spesso più di 10 lingue di sottotitoli e il predefinito `all` le offre
  tutte, per ogni lingua audio: **imposta `--subs` con quelle che leggi**, per esempio
  `--subs ita,eng` (`none` per nessuna versione sottotitolata).
- **I sottotitoli 3D** sono disegnati in entrambi gli occhi, ogni copia spostata verso
  l'interno di `--sub-depth` pixel (predefinito 8), così galleggiano poco davanti allo
  schermo. Aumentalo se sembrano "dentro" la scena, `0` li mette sul piano dello schermo.
- Funzionano anche **file di sottotitoli esterni**: mettili accanto a un ISO/BDMV/MKV con lo
  stesso nome (`film.srt`, `film.ita.srt`, anche `.ass`, `.sup`…). Compaiono accanto a ogni
  video virtuale con il nome abbinato, e il player li carica come sottotitoli esterni (il 3D
  Player VITURE li mostra correttamente in entrambi gli occhi).

## Rete e qualità

Ogni file ha un **bitrate costante**: è quello che permette di far corrispondere un byte del file a un secondo del film. Quindi un file **non può adattarsi alla rete** come fa YouTube. Ogni film viene invece offerto a due bitrate:

| | Normale | `Light/` |
|---|---|---|
| **3D** (Full-SBS 3840×1080) | 24 Mbit/s (video 20) | 10 Mbit/s (video 8) |
| **2D** (1920×1080) | 15 Mbit/s (video 12) | 6,5 Mbit/s (video 5) |
| **DVD** (1024×576 PAL, 854×480 NTSC) | 5 Mbit/s (video 4) | 2,4 Mbit/s (video 1,8) |

Audio: AAC stereo 192 kbit/s per lingua (DTS e TrueHD non sono supportati dalla maggior parte dei player mobili); un file con più lingue (`Multi-audio/`) cresce di 0,22 Mbit/s per ogni lingua in più. Video: H.264.

Se la riproduzione **si ferma ogni pochi secondi**, il Wi-Fi non regge quel bitrate (basta un muro spesso):
- apri lo stesso film da **`Light/`**;
- in **VLC** aumenta la cache di rete (*Impostazioni → Avanzate → Cache di rete*) a 5000-10000 ms: assorbe i brevi cali del Wi-Fi;
- il programma se ne accorge e lo scrive nel suo log:
  ```
  Blu-ray/ITA - Ready Player One.ts: the player receives 77% of the data rate the movie needs:
  the network is too slow for this file, playback will pause (try Light/)
  ```

---

## Risoluzione dei problemi

- **Il film non compare e il log dice `cannot decrypt`**: nessuna chiave funzionante per quel disco. Aggiorna il `KEYDB.cfg`, oppure installa e registra MakeMKV (strada nativa).
- **La riproduzione si ferma ogni pochi secondi**: il Wi-Fi è troppo lento per quel file; vedi [Rete e qualità](#rete-e-qualità).
- **Inserendo il disco non succede niente**: verifica che il tuo utente possa leggere il lettore (`ls -l /dev/sr0`, gruppo `cdrom`) e, con Docker, che il dispositivo sia passato al container.
- **Gli occhiali non vedono la cartella condivisa**: verifica che PC e occhiali siano sulla stessa rete, e controlla il firewall (porta 445/TCP). Da un altro PC Linux: `smbclient -N -L //<ip-del-pc>`.
- **L'immagine va a scatti**: controlla prima il Wi-Fi (5 GHz, vicino al router). Poi il log dell'ultima pipeline (`/tmp/bd3d-pipeline.log`, oppure `docker compose logs`).
- **Un file nuovo in `MOVIES_DIR` non compare**: le cartelle vengono lette all'avvio. Riavvia il programma o il container (i lettori invece sono controllati di continuo).

---

## Come funziona (per i curiosi)

- **Lettura del disco.** libbluray legge il disco (o l'ISO/BDMV) e lo decifra tramite libaacs o la libmmbd di MakeMKV (`src/bluray.py`, un piccolo binding ctypes). Il film è la playlist più lunga le cui clip hanno tutte una vista dipendente MVC (`src/bdmv.py` legge playlist e informazioni delle clip).
- **Le due viste.** Il file `.ssif` di una clip 3D alterna la vista base (PID 0x1011) e quella dipendente (0x1012) a blocchi (extent). `src/ssif_demux.py` accoppia i due pacchetti PES di ogni fotogramma in base al DTS e li scrive in Annex B per edge264. Toglie le unità NAL di separazione, di riempimento e di fine sequenza dei Blu-ray, come fa MakeMKV. Le libbluray più vecchie della 1.4 non aprono i file `.ssif`, quindi gli stessi byte vengono ricostruiti dai due file `.m2ts`.
- **edge264-mvc** decodifica entrambe le viste e le scrive affiancate (`edge264_test -Ok`). FFmpeg da solo non ci riesce: scarta la vista dipendente.
- **Seek.** Un tempo viene tradotto in un byte del `.ssif` attraverso l'EP_map (le posizioni dei fotogrammi chiave) e la tabella degli extent della clip. I film composti da più clip (Tron: due) vengono letti in sequenza, e i timestamp audio delle clip successive vengono riportati su un'unica linea temporale.
- **Audio.** `src/disc_reader.py` legge il disco una volta sola: il video va a edge264, la traccia audio scelta (con la lingua presa dalla playlist) va a FFmpeg attraverso una FIFO, ognuno con il suo thread. Il fotogramma chiave viaggia insieme all'audio come ancora temporale, così audio e video partono esattamente insieme. Scarto misurato rispetto alla strada MKV: 4 ms.
- **Il file virtuale.** L'encoder lavora a **bitrate costante** e il muxer MPEG-TS riempie esattamente fino a 24 Mbit/s, così il byte _X_ è il secondo _X_ / 3.000.000 del film. Un file system **FUSE** (`src/bd3d_fs.py`) serve i file. Riavvia la decodifica quando il player salta e mette in pausa la pipeline se va troppo avanti. Per ogni lettore gira una sola pipeline alla volta, perché due farebbero saltare avanti e indietro il lettore ottico. La fine del file, che i player leggono per ricavare la durata, è sintetica: nero e silenzio con i timestamp giusti, così il lettore non viene mandato alla fine del disco.
- **DVD.** `src/dvd.py` legge il disco tramite libdvdread (libdvdcss per il CSS) e i suoi file IFO: il titolo principale (il più lungo), le sue celle, le lingue audio, lo standard video e il formato. Il seek usa la mappa dei tempi per arrivare entro pochi secondi, poi il pacchetto di navigazione di ogni blocco VOBU (circa 0,5 s), che contiene il suo tempo esatto: il punto di ripartenza è noto con precisione. Il lettore DVD di FFmpeg salta solo in modo approssimativo (secondi di scarto, senza sapere dove è atterrato). `src/dvd_reader.py` manda il titolo da lì a FFmpeg, che lo deinterlaccia e lo porta a pixel quadrati.
- **Lettori.** Ogni pochi secondi si chiede al lettore se c'è un disco (senza leggerlo). All'inserimento il disco viene aperto come Blu-ray, altrimenti come DVD, e il suo film aggiunto; all'espulsione viene tolto.

---

## Limiti

- Provato con **un solo disco 3D** (Tron: Legacy 3D), solo AACS. I dischi con BD+ (tramite MakeMKV) e quelli con strutture insolite non sono provati.
- Gli ultimi 2,7 secondi circa di ogni film (dopo i titoli di coda) sono neri: la fine del file è sintetica.
- Dischi provati: uno per tipo (3D: Tron: Legacy; 2D: Ready Player One; DVD: Ritorno al futuro, PAL). I DVD NTSC non sono provati.
- I sottotitoli del disco sono disegnati nell'immagine (una versione per lingua), non si scelgono dal player; la profondità 3D è fissa, non presa dal disco. L'audio è convertito in AAC stereo.

## E Windows?

Questo progetto è solo per Linux. Su Windows puoi guardare [**SyLC**](https://github.com/5ymph0en1x/SyLC), un player open source che riproduce direttamente l'MVC dei Blu-ray 3D (MKV, ISO, BDMV) e può produrre SBS. Non l'ho provato.

## Riconoscimenti e riferimenti

- [edge264-mvc](https://github.com/jens-duttke/edge264-mvc) (BSD), fork di [edge264](https://github.com/tvlabs/edge264): il decoder MVC che rende possibile tutto questo;
- [libbluray / libaacs](https://www.videolan.org/developers/libbluray.html), [FFmpeg](https://ffmpeg.org/), [Samba](https://www.samba.org/), [pyfuse3](https://github.com/libfuse/pyfuse3), [MakeMKV](https://www.makemkv.com/);
- [Play 3D Blu-ray in SBS directly from the disc…](https://cybereality.com/play-3d-blu-ray-in-sbs-directly-from-the-disc-for-playback-on-3d-monitors-xr-glasses-and-vr-headsets-using-free-and-open-source-tools/) (cybereality): un approccio per Windows (LAV Filters + madVR).
