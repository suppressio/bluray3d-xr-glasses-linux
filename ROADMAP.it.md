[🇬🇧 English](ROADMAP.md) | 🇮🇹 Italiano

# Roadmap

## Stato

**Fatto: il Blu-ray 3D si guarda direttamente dal disco.** Inserisci il disco, il
film compare nella condivisione, si vede in 3D sugli occhiali con seek e audio
funzionanti, e sparisce quando togli il disco. Funziona anche da ISO, cartelle
BDMV e rip MKV. Le fasi qui sotto raccontano come è stato costruito e
verificato, passo per passo, su Tron: Legacy 3D.

Prossimi passi, in quest'ordine:

- [x] **Blu-ray 2D** e **DVD**, ciascuno nella sua cartella della condivisione
  `Disks`. Il demuxer `dvdvideo` di FFmpeg salta solo in modo approssimativo
  (secondi di scarto, senza sapere dove è atterrato), quindi i DVD hanno un
  lettore proprio: libdvdread + lettura degli IFO, seek con i pacchetti di navigazione
  (esatto).
- [x] **Sottotitoli dal disco**, disegnati nell'immagine come fa un lettore da
  salotto (PGS dei Blu-ray, 2D e 3D, sottotitoli dei DVD): una versione per ogni
  lingua, quelli forzati sempre presenti. Estrarli come file esterni vorrebbe dire
  leggere prima tutto il disco, troppo lento. Quelli 3D hanno una profondità fissa.
  Verificati su tutti e tre i tipi di disco.
- [x] **Salti più rapidi.** Dopo un salto il player manda ancora letture per la
  posizione che ha lasciato; su un disco (una pipeline alla volta) facevano
  ripartire la posizione vecchia e fermavano quella nuova. Ora ricevono i dati
  vecchi, e un salto richiede 2-4 s. Animazione di caricamento opzionale
  (`--loader`) al posto dell'immagine ferma durante l'attesa.
- [x] **Un'animazione di caricamento predefinita** senza problemi di diritti: una
  scena retrowave fatta da zero in Blender, affiancata (3D nei film 3D, il suo
  occhio sinistro in quelli 2D), 1 MB.
- [x] **DVD difficili.** I dischi di serie inglesi nascondono gli episodi tra
  decine di titoli finti che ripetono celle rimescolate (una protezione
  anticopia): vengono riconosciuti e ogni episodio diventa un file. I punti
  rovinati vengono saltati, partono anche i titoli che si aprono con secondi senza
  audio, il seek funziona nei titoli che riproducono due volte una cella.
- [ ] **Altri dischi.** Provati: Tron: Legacy 3D, Ready Player One, Cowboy Bebop,
  Ritorno al futuro PAL, Utopia PAL; dischi con BD+, più angolazioni o DVD NTSC
  potrebbero richiedere lavoro.
- [ ] **DLNA in alternativa a Samba.** Gli stessi file virtuali serviti via
  HTTP e annunciati con DLNA/UPnP, per i player che sfogliano un media server
  invece di aprire una condivisione (il lettore video del Pico, le TV, Moon VR,
  Kodi...). Un'opzione all'avvio: Samba, DLNA o entrambi. Si innesta bene su
  com'è fatto: il file ha un bitrate costante, quindi una richiesta HTTP di un
  intervallo di byte equivale a una lettura del file FUSE, e il seek continua a
  funzionare. Con il solo DLNA non servono né FUSE né Samba.
- [ ] **Windows e macOS.** Il cuore (lettura del disco, demux, seek, pipeline)
  è già portabile; le poche parti legate a Linux vanno prima dietro un piccolo
  strato di piattaforma, su Linux, con risultati identici nei test. Poi prove
  con un lettore USB su un Mac e su un PC Windows.

Più avanti, forse:
- **visione di coppia**, due persone con i propri occhiali, sincronizzate: un
  flusso live condiviso (RTSP/HLS con mediamtx) con pausa e salti comuni da un
  telecomando web. Due player che leggono lo stesso file funzionano già, ma
  ognuno ha la sua posizione. Limiti: il 3D Player VITURE apre solo file SMB,
  non flussi di rete, e player separati restano a circa 1 s di distanza a meno
  che supportino un protocollo di sincronizzazione;
- una modalità "passthrough" per i dischi 2D: servire il flusso originale senza
  ricodifica (qualità piena, ma il seek dipende di più dal player);
- codifica NVIDIA dentro Docker (il file compose c'è, non è provato);
- codifica VAAPI per le GPU Intel e AMD (`h264_vaapi`, non provata: qui non ce
  ne sono). Senza NVIDIA codifica la CPU (x264): su una CPU desktop recente a
  12 core la sola codifica 3D va a 1,4x il tempo reale con 2 core, 3,5x con tutti e 12;
- un pacchetto `.deb` per Debian/Ubuntu, costruito da GitHub Actions a ogni
  release e allegato a essa: `apt install ./bluray3d-xr_….deb` installa anche le
  dipendenze, `apt remove` toglie anche la condivisione Samba. edge264 compilato
  per le distribuzioni (x86-64-v2/v3, istruzioni della CPU scelte a runtime), con
  la sua licenza BSD.

## La decifratura

I dischi commerciali usano AACS (e alcuni anche BD+). libbluray decifra tramite
un plugin scelto al momento dell'esecuzione, quindi il progetto non deve
dipendere da un solo strumento. Tre opzioni, provate in quest'ordine (il
progetto **non distribuisce e non scarica mai chiavi**):

1. **libaacs + KEYDB.cfg**: tutto open source (VideoLAN). L'utente mette un
   database di chiavi in `~/.config/aacs/` e lo tiene aggiornato (i dischi
   nuovi revocano le chiavi vecchie; alcuni lettori chiedono anche un
   certificato "host"). Si usa quando viene trovato un KEYDB.
2. **libmmbd** (di MakeMKV): sostituisce libaacs/libbdplus usando le chiavi di
   MakeMKV (`LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd`). Apre quasi tutto,
   BD+ compreso, ma è proprietario: al suo interno lancia `makemkvcon`, quindi
   MakeMKV deve essere installato, funzionante e registrato (chiave beta
   gratuita, da rinnovare periodicamente).
3. **ISO o cartella BDMV già decifrati**: nessuna chiave mentre guardi (il
   disco è stato decifrato una volta, con qualsiasi strumento). È una copia
   completa (circa 45 GB), quindi di nuovo un passaggio intermedio, ma senza
   dipendenze durante la visione.

Nota legale: in molti paesi (Italia e gran parte dell'UE compresi) aggirare una
protezione anticopia non rientra nell'eccezione per la copia privata,
qualunque strumento si usi.

Risultato della Fase 0 sul PC di prova (Tron: Legacy 3D, 28/09/2026): **funzionano tutte e tre le opzioni**:

| Opzione | AACS | SSIF decifrato | Note |
|---|---|---|---|
| libaacs + KEYDB.cfg | gestito (MKB v19) | ✅ 64 MB in circa 7 s, avvio del disco compreso | tutto open source |
| libmmbd (MakeMKV 2.0.0) | gestito | ✅ circa 15 s (avvia `makemkvcon`) | stesso contenuto di libaacs; cambiano solo i bit di permesso di copia nell'intestazione di ogni pacchetto |
| cartella BDMV decifrata | assente | ✅ identico byte per byte al disco | nessuna libreria di chiavi caricata |

Cosa abbiamo scoperto: il disco è 3D, solo AACS (niente BD+). La playlist
principale 00070 è fatta delle clip 00131 + 00133, e la vista base è l'occhio
sinistro. Il file SSIF decifrato contiene il PID `0x1011` (vista base, H.264
High 1920×1080), il PID `0x1012` (vista dipendente MVC), l'audio e i
sottotitoli PGS. Il lettore con decifratura accetta solo letture di esattamente
un'unità AACS (6144 byte). MakeMKV compilato per FFmpeg 7 non parte sui sistemi
con FFmpeg 8 (va ricompilato makemkv-oss). La chiave beta va scritta in
`~/.MakeMKV/settings.conf` (`app_Key = "T-..."`): `makemkvcon reg` la rifiuta.

## Fasi (come è stato costruito)

- [x] **Fase 0 — Accesso**, una volta per ogni opzione di decifratura (libaacs,
  libmmbd, BDMV decifrato). Con il disco nel lettore: libbluray lo apre,
  elenca le playlist e trova quella 3D principale. Il file SSIF della sua
  clip si legge decifrato con `bd_open_file_dec`, e ffprobe su quei byte vede
  sia `0x1011` sia `0x1012`. _Criterio per decidere se andare avanti: il disco
  si decifra con almeno un'opzione._
- [x] **Fase 1 — Demux.** `src/ssif_demux.py` trasforma il transport stream
  SSIF in Annex B, con le unità NAL base e dipendenti di ogni fotogramma in
  ordine di decodifica: è ciò che oggi edge264 riceve dall'MKV. Come funziona:
  ogni fotogramma è un PES per vista e i due hanno lo stesso DTS, quindi i
  fotogrammi vengono accoppiati per DTS e scritti nell'ordine della vista base.
  Il separatore Blu-ray (NAL di tipo 24) viene tolto, come fa MakeMKV.
  Risultato su Tron: Legacy 3D (disco decifrato con libaacs): **3730 fotogrammi
  SBS decodificati su 3730 identici bit per bit** a quelli della strada MKV
  (framemd5 dell'uscita di edge264, primi 2,6 minuti). Velocità del demux:
  circa 390 MB/s in Python, contro i circa 6 MB/s necessari.
- [x] **Fase 2 — Seek.** `src/bdmv.py` legge la playlist (MPLS, con il sotto
  percorso 3D che indica le clip dipendenti) e le informazioni delle clip
  (CLPI: EP_map e punti di inizio degli extent). `ssif_seek()` traduce un
  tempo nell'entry point (fotogramma chiave) della vista base, poi nell'extent
  che lo contiene. La lettura parte dall'extent dipendente che lo precede,
  perché il file .ssif alterna D0 B0 D1 B1 …, e il demuxer scarta i
  fotogrammi prima di quello chiave. `src/bluray.py` è un piccolo binding
  ctypes a libbluray (apertura, lettura decifrata, salto a unità di 6144 byte).
  Risultato su Tron: Legacy 3D a 5, 30 e 50 minuti: stesso fotogramma chiave
  della strada MKV (entro 3 ms, la precisione dell'EP_map), **tutti i
  fotogrammi SBS decodificati identici** a quelli dell'MKV partito da lì.
  Salto + lettura di 48 MB dal lettore: 2-3,5 s. Entry point in media ogni
  0,9 s (al massimo ogni 2 s).
  Nota per la Fase 3: allineare l'audio sul PTS reale del primo fotogramma,
  non sul tempo dell'EP_map (arrotondato a 5,7 ms).
- [x] **Fase 3 — Audio.** `src/disc_reader.py` legge il disco una volta e
  divide il flusso. Il video Annex B va su stdout, verso edge264. La traccia
  audio scelta va, come piccolo MPEG-TS, in un file o in una FIFO per ffmpeg.
  Ognuno ha il suo thread e la sua coda: ffmpeg apre gli input uno alla volta
  e non legge il video mentre analizza l'audio, e con un unico lettore
  bloccante la catena si fermava. Le lingue audio si leggono dalla tabella STN
  della playlist (Tron: `0x1102` = italiano DTS). Tre dettagli: (1) il file
  .ssif contiene due PMT sullo stesso PID (clip base e dipendente), quindi si
  inoltra solo quella base, filtrata sulle tracce inoltrate (con nuovo CRC);
  altrimenti ffmpeg aspetta tracce che non arrivano mai. (2) Il video viaggia
  in anticipo sul suo tempo di presentazione, quindi l'audio parte dal primo
  PES con PTS ≥ a quello del fotogramma chiave. (3) Il fotogramma chiave
  stesso viene inoltrato nel TS dell'audio come ancora temporale (mai usato
  come traccia), così per ffmpeg quell'input parte esattamente dal tempo del
  video. Risultato a 50 minuti rispetto alla strada MKV: 719 fotogrammi video
  su 719 identici, scarto dell'audio 3,9 ms.
- [x] **Fase 4 — `BlurayDiscSource`.** Lettore, ISO e cartella BDMV come
  sorgenti (`src/sources.py`). **Un file virtuale per ogni film** (la playlist
  3D principale), con il nome preso dai metadati del disco. Per un lettore il
  file **compare quando inserisci un disco e sparisce quando lo togli**: ogni 3
  secondi si chiede al lettore se c'è un disco, senza leggerlo. La decifratura
  prova libaacs, poi libmmbd. I titoli fatti di più clip vengono letti in
  sequenza (Tron: 2); i timestamp audio delle clip successive vengono riportati
  sulla linea temporale della prima.
  Cosa ha insegnato l'uso vero:
  - le clip dei Blu-ray finiscono con riempitivo e un NAL di fine sequenza; dopo
    quest'ultimo edge264 rifiutava la clip successiva. Il demuxer ora toglie i
    NAL 10/11/12, come MakeMKV;
  - il primo fotogramma chiave può stare qualche millisecondo prima dell'inizio
    ufficiale della play item; l'inizio del film finiva a un tempo negativo e
    non partiva niente;
  - i player leggono la fine del file (per la durata) mentre riproducono
    l'inizio. Con una catena per file questo fermava la riproduzione; con due,
    il lettore ottico saltava avanti e indietro e andavano piano entrambe. Ora
    le letture interrotte riprovano invece di restituire niente, gira una
    catena per disco, e la fine del file è sintetica (nero + silenzio con i
    timestamp giusti);
  - libbluray < 1.4 (Debian 13, Ubuntu 24.04) non apre i file `.ssif`: vengono
    ricostruiti dai due `.m2ts`, identici byte per byte.
  Risultato: visto dal disco sul Neckband VITURE, seek funzionanti (qualche
  secondo, per il lettore), passaggio tra le clip senza interruzioni,
  espulsione/reinserimento funzionanti (il file torna 14 s dopo la chiusura
  dello sportello).
- [~] **Fase 5 — Prova lunga su un lettore vero.** Fatto: Tron: Legacy 3D,
  riproduzione, seek, espulsione/reinserimento. Da fare: un film intero tutto
  di seguito, altri dischi.
