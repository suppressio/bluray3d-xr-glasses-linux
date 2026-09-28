[🇬🇧 English](ROADMAP.md) | 🇮🇹 Italiano

# Roadmap: dal disco, senza rip

Oggi il progetto parte da un **rip MKV** che ha conservato il 3D (MVC). È utile se
questi rip li hai già e non vuoi convertirli, ma è un passaggio in più del
necessario: chi ha il disco e accetta di convertire può già copiarlo
direttamente in SBS con gli strumenti esistenti.

Il vero obiettivo è **leggere il Blu-ray 3D stesso** (disco nel lettore, ISO o
cartella BDMV), decifrarlo e decodificarlo al volo, e servirlo agli occhiali
esattamente come oggi: niente rip, niente conversione, niente spazio su disco.

## Cosa c'è già

- **La pipeline non sa da dove arriva il film.** `src/sources.py` definisce una
  `Source`, che deve fornire quattro cose: un flusso H.264 Annex B con la vista
  dipendente MVC, l'audio, la durata e il keyframe da cui partire per un seek.
  `MkvSource` le fornisce per i rip; una `BlurayDiscSource` dovrà fornire le
  stesse quattro cose. Decoder, encoder, file virtuale e condivisione SMB
  restano come sono.
- **Già disponibili su Debian**: libbluray (con `bd_open_file_dec`,
  `bd_get_clpi`, `bd_get_playlist_info`), libudfread, libaacs, libbdplus.

## Il problema aperto: la decifratura

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

## Fasi

In ordine dalla più economica alla più costosa, per potersi fermare presto.

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
- [ ] **Fase 2 — Seek.** Tradurre un tempo in una posizione nel file SSIF: EP_map
  nel CLPI (tempo → pacchetto) più la disposizione degli extent SS, oppure una
  ricerca binaria sui PTS. Misurare quanto è preciso. Mantenere la lezione
  imparata con l'MKV: video e audio devono partire esattamente dallo stesso
  keyframe.
- [ ] **Fase 3 — Audio.** Prendere il PID audio scelto dallo stesso flusso
  decifrato e passarlo all'encoder. Vuol dire estendere `Source.audio_input`
  oltre "un file che ffmpeg sa aprire" (per esempio una FIFO alimentata dal
  programma di supporto).
- [ ] **Fase 4 — `BlurayDiscSource`.** Lettore, ISO e cartella BDMV come
  sorgenti. Nome del film dai metadati del disco, playlist composte da più
  clip, riconoscimento dei titoli 2D e 3D.
- [ ] **Fase 5 — Prova lunga su un lettore vero.** Un film intero più seek
  avanti e indietro. Verificare la velocità di lettura del lettore: il file
  SSIF richiede circa 1,3 volte la velocità 1× dei Blu-ray, alla portata di
  qualsiasi lettore, ma i tempi di avvio del disco e dei salti di un lettore
  ottico si fanno sentire. Provare più dischi, non uno solo.

## Rischi

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Chiavi di decifratura (revoche AACS, BD+) | dischi nuovi che smettono di aprirsi anche con il codice giusto | appoggiarsi a libmmbd/MakeMKV o a un KEYDB mantenuto dall'utente; mai distribuire chiavi |
| Errori nel demux (base e dipendente interlacciate male) | 3D sbagliato senza errori visibili | la Fase 1 confronta con l'MKV fotogramma per fotogramma |
| Precisione e lentezza del seek su un lettore vero | esperienza peggiore rispetto all'MKV | misurarle nelle Fasi 2 e 5 prima di dichiarare un miglioramento |
| Dischi con struttura insolita (seamless branching, più clip) | alcuni titoli non funzionano | Fase 4, prove su più dischi |
| Impegno di sviluppo | tempo speso senza risultato | fasi ordinate per fermarsi presto |

## Fermarsi se

- i dischi che contano non si decifrano con libmmbd / libaacs;
- la Fase 1 si rivela sproporzionata rispetto al beneficio;
- la strada MKV basta per l'uso reale.
