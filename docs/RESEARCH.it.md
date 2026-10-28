🌐 [English](RESEARCH.md) | **Italiano**

# Ricerca sul Blu-ray: playlist e scelta dei titoli

> Note di ricerca, raccolte il 2026-10-10 e il 2026-10-13 mentre si costruiva la scelta dei titoli per i Blu-ray con più episodi. Fatti sul formato delle playlist Blu-ray e su come altri programmi scelgono i titoli, ciascuno con la sua fonte; il formato non è pubblico, quindi vengono da documentazione ricostruita (reverse engineering), codice sorgente e forum. Il *Riepilogo* è la versione breve, le sezioni dopo contengono i risultati completi. Le citazioni testuali dalle fonti restano in inglese. Il codice che si basa su un risultato cita questo file (per esempio `src/sources.py`, Q4).

## Riepilogo

Il formato MPLS del Blu-ray non ha alcun campo che classifichi una playlist come film, episodio, play-all o variante di lingua. Il blocco `AppInfoPlayList` espone solo flag operativi (riproduzione sequenziale o casuale, restrizioni sulle operazioni dell'utente, permessi di mix audio e un solo bit per il 3D stereo) e lascia la classificazione del contenuto interamente alle euristiche dei programmi che leggono il disco. libbluray, MakeMKV e HandBrake hanno ciascuno il proprio filtro strutturale basato sull'identità delle clip dei PlayItem, sulla durata totale e sul numero di capitoli; nessuno espone un segnale "episodio" garantito. Per il codice che deve riconoscere gli episodi su un BD, l'indicatore strutturale più affidabile è la segment map (l'elenco delle clip dei PlayItem), usata insieme a soglie di durata e al numero di capitoli, e ancorata alla tabella dei titoli autorevole in `MovieObject.bdmv`. Le playlist play-all gestite da Java e i dischi con tutti gli episodi in un unico segmento restano opachi all'ispezione statica degli MPLS.

### Il formato binario MPLS contiene solo flag operativi

Un file MPLS inizia con il magic ASCII di 4 byte `MPLS`, una stringa di versione di 4 byte (di solito `0200`) e tre offset big-endian a 32 bit verso le sezioni PlayList, PlayListMark ed ExtensionData (facoltativa); tutti gli interi sono big-endian ([Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)). Il blocco `AppInfoPlayList`, all'offset `0x28`, è quasi sempre lungo 14 byte e contiene tre campi utili per il riconoscimento: un **byte `playback_type`** (`1` = sequenziale, `2` = casuale, `3` = shuffle), un `playback_count` di 2 byte usato solo in modalità casuale/shuffle, e un byte `miscellaneous_flags` i cui bit sono `random_access_flag`, `audio_mix_flag`, `lossless_bypass_flag` e, da una patch di libbluray di LAV, `mvc_base_view_r_flag` (l'orientamento della vista base MVC per i dischi 3D) ([LAV libbluray commit](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)). Non esiste un campo `content_type`, `playlist_type` o `application_type` a livello di playlist; la specifica non è pubblica e questa assenza nella documentazione ricostruita è confermata da tutti i parser open source ([docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)).

Un'informazione sul tipo di contenuto esiste a livello di clip, non di playlist. Ogni file CLPI (ClipInfo) ha un byte `clip_stream_type` e un **byte `application_type`** nel blocco `ClipInfo`; i valori di `application_type` da 1 a 7 distinguono i transport stream del percorso principale (main path) da quelli dei percorsi secondari (sub path) come menu, slideshow e sottotitoli testuali: il valore 1 indica la clip del main path di un film, il 6 una clip di sottotitoli testuali in un sub path ([libbluray clpi_data.h](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)). Il punto decisivo: `application_type = 1` vale allo stesso modo per un film, un episodio TV e un contenuto extra; la distinzione fra queste tre categorie non esiste in nessuna struttura binaria dello standard. Ogni classificazione è un'inferenza fatta dopo.

La sezione `PlayListMark` aggiunge i capitoli: ogni voce di 14 byte contiene un `mark_type` (`1` = Entry Mark / capitolo, `2` = Link Point), l'ID di un PlayItem, un timestamp in **45000esimi di secondo**, il PID di un elementary stream (di solito `0xFFFF`) e una durata (`0` = nessuna durata fissa) ([Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)). Numero e posizione dei capitoli si leggono quindi direttamente dall'MPLS, senza toccare le clip M2TS.

### Segnali strutturali usati al posto del tipo di contenuto

Poiché la specifica non ha un enum, ogni programma ripiega su quattro indicatori strutturali: la **segment map** (l'elenco ordinato dei nomi delle clip M2TS a cui puntano i PlayItem di una playlist), la **durata totale**, il **numero di PlayItem** e il **numero di capitoli**.

La segment map è il segnale che discrimina di più. La playlist di un singolo episodio punta a una o due clip; la playlist play-all di un disco con cinque episodi elenca in ordine tutte e cinque le clip: una segment map `"1,2,3,4,5"` contro `"2"` distingue senza ambiguità i due tipi quando il disco è fatto bene ([MakeMKV forum, segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)). Poiché le clip M2TS degli episodi non sono sempre numerate nell'ordine degli episodi (disco della stagione 1 di Westworld: la playlist 105 viene prima della 107, che viene prima della 103), la playlist play-all è anche l'unico modo affidabile di ricavare la sequenza degli episodi dalla sola struttura del disco ([MKVToolNix forum](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)). Una volta trovata la play-all, l'ordine dei suoi PlayItem dà direttamente l'ordine corretto degli episodi.

Le varianti di lingua complicano le cose. I dischi Disney usano la serie di MPLS `00800` per le varianti per lingua: `00800.mpls` è l'inglese, `00801.mpls` lo spagnolo, `00802.mpls` il francese; condividono quasi tutte le clip ma sostituiscono un piccolo gruppo di clip localizzate per titoli di testa e di coda ([MakeMKV forum, Rogue One thread](https://forum.makemkv.com/forum/viewtopic.php?p=55134)). È una convenzione degli studi (la serie `00800`+ non ha un significato particolare nella specifica) e cambia da studio a studio. La fonte autorevole per l'associazione playlist → titolo è `MovieObject.bdmv`, che contiene i veri comandi di riproduzione (`"play playlist XYZ"`), mentre `index.bdmv` associa i numeri di titolo del disco ai movie object ([MKVToolNix forum, mbunkus reply](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)). Per trovare la playlist "vera" di un disco, leggere `MovieObject.bdmv` è più affidabile e più corretto che tirare a indovinare dalla numerazione degli MPLS.

### Il filtro di libbluray: cosa fa davvero TITLES_RELEVANT

L'API pubblica di libbluray offre tre flag di filtro per `bd_get_titles()`: **`TITLES_ALL = 0`**, **`TITLES_FILTER_DUP_TITLE = 0x01`** e **`TITLES_FILTER_DUP_CLIP = 0x02`**; `TITLES_RELEVANT = 0x03` è il loro OR bit a bit ([libbluray bluray.h](https://videolan.videolan.me/libbluray/bluray_8h_source.html)). Il terzo argomento, `min_title_length`, è una durata minima in secondi. La logica sta in `navigation.c`, non nel parser MPLS.

`_filter_dup()` fa un confronto strutturale: confronta `list_count` (numero di PlayItem) e, per ogni PlayItem, il nome della clip con `in_time` e `out_time`. Se tutto coincide, la seconda playlist viene scartata come duplicato ([libbluray-devel mailing list, 2014](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001457.html)). Una patch proposta da HandBrake nel 2014 estendeva il confronto agli attributi di ogni stream (tipo di stream, tipo di codifica, PID, codice lingua e numero di stream per play item); questo confronto più severo tratterebbe come duplicati le varianti di lingua, mentre quello di base le conserva perché PID audio diversi rendono diverse le strutture delle clip. Non è confermato se la patch sia entrata in libbluray upstream o viva solo in fork a valle (Nixpkgs, LAV).

`_filter_repeats()` / `_find_repeats()` mirano a un'altra forma: **una sola playlist che punta più volte alla stessa clip con gli stessi tempi di in/out in più PlayItem**, struttura tipica dei menu in loop o delle esche anti-copia più che di contenuti veri. La soglia esatta di ripetizioni non si è trovata nelle fonti disponibili. Un commit di libbluray del 2019 ha aggiunto la preferenza per il codec HEVC come criterio di spareggio nella scelta del titolo principale ([libbluray commit history](https://code.videolan.org/videolan/libbluray)).

La conseguenza per il riconoscimento della play-all è controintuitiva: `TITLES_FILTER_DUP_CLIP` può *togliere* la playlist play-all oppure le playlist dei singoli episodi a seconda dell'ordine in cui il filesystem le restituisce, perché entrambi i gruppi puntano alle stesse clip M2TS. libbluray non ha un'API per dire "questa è una play-all"; l'effetto è un'eliminazione strutturale, non una classificazione. Il `bluray.c` di FFmpeg chiama `bd_get_titles(bd, TITLES_RELEVANT, 180)` e poi sceglie il titolo **più lungo** ([FFmpeg bluray.c, camenduru mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)): un'euristica che funziona per i film ma sbaglia in silenzio sui dischi di serie TV, dove più episodi hanno la stessa durata.

### MakeMKV e HandBrake: strategie diverse sulla stessa base

Lo strato di analisi binaria di MakeMKV è closed source. Dal comportamento visibile si vede un controllo di identità della segment map simile a `_filter_dup` di libbluray: quando due playlist puntano alla stessa sequenza ordinata di clip M2TS, MakeMKV scrive nel log `"title 6 is the same as title 7, and was therefore skipped"` ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=52406)). Per i dischi con **offuscamento delle playlist via Java**, MakeMKV usa un Java Runtime incluso per eseguire il codice BD-J del disco e marca il film principale trovato con **`(FPL_MainFeature)`** nell'elenco dei titoli ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=95219)); la versione 1.16.4 (2021) ha portato "much better support for discs with Java playlist obfuscation" ([VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)). Senza Java, MakeMKV mostra tutte le playlist che passano il suo filtro strutturale e lascia all'utente il compito di associare durate e segment map agli episodi.

HandBrake usa libbluray direttamente, tramite `libhb/bd.c`. Passa **sempre** `TITLES_FILTER_DUP_CLIP` e aggiunge `TITLES_FILTER_DUP_TITLE` a meno che l'utente non imposti `--keep-duplicate-titles` (aggiunto in HandBrake 1.9.0, [PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)). Passa `min_title_length = 0`, quindi anche playlist sotto il secondo superano la soglia di durata. Dopo la scansione **ordina i titoli per numero di MPLS** (comparatore `title_info_compare_mpls`), quindi il titolo di indice 0 è sempre il file `.mpls` con il numero più basso, non il più lungo. `hb_bd_main_feature()` sceglie il candidato film principale con la **regola del 70% della durata**: è candidato ogni titolo lungo almeno il 70% del più lungo; fra i candidati, gli spareggi si decidono sul rango del formato video (preferiti HEVC e codec di qualità più alta), poi sulla durata, poi sul numero di capitoli ([HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)). HandBrake non riconosce gli episodi; su un disco di serie TV l'euristica del film principale sceglie di solito la play-all come titolo predefinito da codificare, perché è il più lungo.

### Casi limite noti che battono ogni euristica

Cinque forme strutturali, documentate su dischi reali, mettono in crisi gli approcci semplici basati su segment map o durata.

**Playlist finte (esche)** sono la tecnica anti-copia più diffusa. Titoli come *John Wick* contengono più di 300 file MPLS quasi identici che puntano alle stesse clip M2TS in permutazioni diverse ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=173579)); `_filter_dup` di libbluray toglie le permutazioni con clip identiche ma non quelle che differiscono anche solo per un tempo di in/out. Per navigare bene questi dischi serve il riconoscimento FPL via Java di MakeMKV.

**Dischi con tutti gli episodi in un solo segmento** mettono tutti gli episodi in un'unica lunga clip M2TS e affidano i confini fra episodi ai marker dei capitoli. La stagione 7 australiana di *Adventure Time* ha questa struttura; il titolo 11 della stagione 1 di *Rick and Morty* è descritto come "a combination of all episodes in 1 large file" ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=56602)). Un'euristica sul numero di segmenti dà `1` sia per la playlist di un episodio sia per questo tipo di play-all; li distingue solo la durata (molto più lunga di un episodio) insieme a un numero alto di capitoli.

**Play-all BD-J in Java** non hanno alcuna struttura MPLS corrispondente. La funzione "Play All" è codice Java che mette in sequenza le playlist dei singoli episodi durante la riproduzione; i programmi che non eseguono BD-J (quasi tutti quelli su Linux) non hanno modo di riconoscere o rappresentare questa forma ispezionando i file ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53072)).

**Varianti dello stesso episodio con durata identica** battono i filtri basati sulla durata. Il disco 3 del Blu-ray di *Band of Brothers* ha quattro titoli di esattamente 1:07:20 e altri quattro di esattamente 55:42; le quattro varianti di ogni episodio si distinguono per "hidden tracks" (tracce audio o angolazioni alternative), non per la durata ([VideoHelp forum](https://forum.videohelp.com/threads/350950-Which-Title)). Un'euristica che guarda solo la durata non può scegliere fra loro.

**Falsi positivi del filtro dei duplicati** fanno perdere episodi. Un utente con il disco 3 della stagione 1 di *Transformers Prime* ha confermato che un episodio era stato scartato dal filtro dei duplicati di MakeMKV ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63515)); sul disco 2 della stagione 15 di *South Park* l'episodio 4 non compare con nessuna delle versioni di MakeMKV provate ([MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63510)). Questi casi mostrano che `TITLES_FILTER_DUP_CLIP` può fondere playlist di episodi davvero distinti quando le loro strutture di clip si sovrappongono.

### Conclusione

Da queste prove segue il progetto pratico di un riconoscimento robusto degli episodi su BD. **Tre segnali, in ordine di affidabilità:** primo, confrontare gli elenchi di clip dei PlayItem (segment map) per individuare le playlist con più clip come candidate play-all e quelle con una sola clip come candidate episodio; secondo, applicare una durata minima tarata sulla lunghezza attesa di un episodio (il default di `bluback`, validato dalla comunità, è 900 secondi, adatto alle serie drammatiche; per gli anime servono circa 1200 secondi); terzo, usare il numero di capitoli come spareggio per distinguere le play-all in un solo segmento dai veri episodi singoli con pochi capitoli. Per l'ordine degli episodi, ricavare la sequenza dei PlayItem dalla play-all invece di fidarsi dei numeri dei file MPLS, che non hanno un ordine né per specifica né per convenzione degli autori.

Due categorie di dischi richiederanno sempre l'esecuzione di BD-J o un intervento manuale: le play-all gestite da Java e i dischi con playlist finte molto offuscate. In un processo automatico la protezione più pratica è scrivere nel log l'elenco completo dei titoli con le segment map e segnalare ogni disco in cui il numero di titoli filtrati si discosta molto dal numero di episodi atteso: così si dice che il disco va trattato a parte, invece di produrre in silenzio un risultato sbagliato.

## Struttura delle playlist MPLS del Blu-ray

### Q1: Quali campi contiene un file MPLS del Blu-ray? Esiste un campo "playlist type" o "application type" che distingua film principale / episodio / play-all / menu?

#### In breve
Nel blocco AppInfoPlayList degli MPLS non c'è un campo `playlist_type` o `application_type` che classifichi il contenuto come "film principale", "episodio" o "play-all". L'unico flag di modalità è `playback_type` (1=sequenziale, 2=casuale, 3=shuffle), che descrive l'ordine di riproduzione, non il significato del contenuto.

#### Risultati con fonte
- Struttura dell'intestazione MPLS: magic ASCII di 4 byte "MPLS", versione di 4 byte (di solito "0200"), poi offset big-endian a 32 bit verso le sezioni PlayList, PlayListMark ed ExtensionData (offset zero = assente). Tutti gli interi a più byte sono big-endian. — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- Il campo lunghezza del blocco AppInfoPlayList, agli offset $28–$2B, vale quasi sempre 0x0000000E (14 byte). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- Campi di AppInfoPlayList agli offset documentati: `playback_type` (1 byte a $2D: 1=Standard/Sequenziale, 2=Casuale, 3=Shuffle), `playback_count` (2 byte a $2E–$2F, usato solo in modalità casuale/shuffle), `UO_mask_table` (8 byte a $30–$37, restrizioni sulle operazioni dell'utente, struttura interna non documentata pubblicamente), `miscellaneous_flags` (1 byte a $38: bit 7 = random_access_flag, bit 6 = audio_mix_flag, bit 5 = lossless_bypass_flag). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls); confermato da [libbluray mpls_data.h via centos mirror](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/mpls_data.h)
- In libbluray la struct di AppInfoPlayList si chiama `MPLS_AI` e aggiunge un bit `mvc_base_view_r_flag` (vista base MVC a destra) nella posizione che la tabella di Wikibooks segna come bit riservato 4; con questo bit i bit riservati finali da saltare passano da 13 a 12. — [LAV libbluray commit "Export the MPLS MVC_Base_View_R_flag"](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)
- Sezione PlayListMark: ogni mark di 14 byte contiene un byte riservato, un `mark_type` (1=Entry Mark / capitolo, 2=Link Point), un `PlayItem ID` (2 byte), un `time_stamp` (4 byte in 45000esimi di secondo), un `entry_ESPID` (2 byte, di solito 0xFFFF) e una `duration` (4 byte, 0 = nessuna durata fissa). — [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- Negli MPLS nessun campo identifica il contenuto come "film principale", "episodio" o "play-all". Il formato MPLS non è pubblicato ufficialmente; i parser si basano su specifiche di terzi, fra cui il repository lw/BluRay e la pagina Wikibooks bdinfo/mpls. — [docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)

#### Deduzioni
- La classificazione del contenuto (film principale, episodio, extra) è dedotta dai programmi con euristiche su durata, numero di PlayItem, nomi delle clip e tabella dei titoli di index.bdmv, non da un enum definito dalla specifica nel file MPLS.

#### Lacune
- La disposizione interna dei bit di UO_mask_table non è documentata pubblicamente in nessuna fonte trovata; la pagina Wikibooks la lascia come "Description pending."
- La struct `MPLS_AI` completa dall'attuale `mpls_data.h` di libbluray upstream non è stata recuperata (git.stg.centos.org e code.videolan.org hanno risposto 404 o accesso negato).

---

### Q2: Com'è fatta la struttura AppInfoPlayList del BDMV? Contiene un playlist_type o un enum simile?

#### In breve
`AppInfoPlayList` (struct `MPLS_AI` in libbluray) contiene solo flag operativi (modalità di riproduzione, restrizioni UO, permessi di mix audio e un flag per il 3D), nessun enum che classifichi il contenuto della playlist.

#### Risultati con fonte
- Campi di `MPLS_AI` confermati nel sorgente di libbluray: `random_access_flag` (1 bit), `audio_mix_flag` (1 bit), `lossless_bypass_flag` (1 bit), `mvc_base_view_r_flag` (1 bit). Sono gli unici flag con nome oltre al byte playback_type e alla UO mask. — [LAV libbluray commit](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)
- `BLURAY_TITLE_INFO` (la struct dell'API pubblica di libbluray) espone: `idx` (indice del titolo), `playlist` (numero della playlist), `duration`, `clip_count`, `angle_count`, `chapter_count`, `mark_count`, `clips`, `chapters`, `marks`, `mvc_base_view_r_flag`, `sdr_conversion_notification_flag`. Non c'è un campo `playlist_type` o `content_type`. — [libbluray bluray.h doxygen source](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- La struct `BLURAY_TITLE` (a cui rimanda `BLURAY_DISC_INFO`) ha un flag `bdj` (1 se è un titolo Java BD-J) e un flag `hidden` (1 se non va mostrato nell'interfaccia). Sono gli unici flag di classificazione per titolo esposti pubblicamente. — [BLURAY_DISC_INFO doxygen](https://videolan.videolan.me/libbluray/structBLURAY__DISC__INFO.html)
- Il file CLPI (`ClipInfo`) ha un byte `clip_stream_type` e un byte `application_type` nel blocco ClipInfo. I valori di `application_type` da 1 a 7 distinguono i transport stream del main path da quelli dei sub path (sottotitoli testuali, menu, slideshow, ecc.). Sono attributi della clip, non della playlist. — [Web search result citing libbluray clpi_dump.c](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/devtools/clpi_dump.c)

#### Deduzioni
- Poiché `application_type` sta a livello di CLPI (clip) e non di MPLS (playlist), una sola playlist può puntare a clip di tipi diversi (per esempio una clip video del main path e una clip di sottotitoli testuali in un sub path).

#### Lacune
- I valori numerici esatti e le etichette di `application_type` nei CLPI (la tabella da 1 a 7) non sono stati recuperati perché il sorgente di clpi_dump.c ha risposto 404 a ogni tentativo. Che esistano si sa dagli estratti dei risultati di ricerca, ma non è stato possibile verificarli.

---

### Q3: I numeri delle playlist (00xxx, 01xxx, 0xxxx) seguono una convenzione o una specifica? Numeri più alti indicano configurazioni audio/sottotitoli alternative?

#### In breve
I numeri delle playlist li assegnano gli autori del disco, senza uno schema imposto dalla specifica. Esistono abitudini diffuse (Disney usa 00800+ per le varianti di lingua), ma sono convenzioni degli studi, non parte della specifica BDMV.

#### Risultati con fonte
- I file MPLS hanno come nome un numero di cinque cifre con zeri iniziali e stanno in `BDMV/PLAYLIST/`. Nessuna specifica stabilisce quale numero contenga il film principale. — [fileinfo.com .MPLS extension](https://fileinfo.com/extension/mpls)
- 00001.mpls è spesso il primo candidato da controllare, ma non è sempre il film principale. Sul disco di Angry Birds 2, 00001 era più corto (1:28:43) delle altre playlist candidate da 1:36:47, quindi era la scelta sbagliata. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80956)
- Un'euristica pratica è aprire `index.bdmv` in un player e annotare quale playlist apre; "99% of the time, this is the correct playlist." — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80956) (affermazione della comunità, non della specifica)
- I dischi Disney usano le playlist 00800+ per le varianti di lingua. Su un disco di Rogue One, 00800 è la versione inglese, 00801 la spagnola, 00802 la francese. Ogni variante condivide quasi tutte le clip ma sostituisce un piccolo gruppo di clip localizzate. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=55134); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=55723)
- I dischi Lionsgate sono a volte descritti come offuscati con molte playlist quasi identiche. I candidati consigliati da controllare sono 00800, 00801 e 00001. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=142684) (osservazione della comunità)
- La vera associazione playlist → titolo è scritta in `MovieObject.bdmv`, che contiene comandi di riproduzione come "play playlist number XYZ". `index.bdmv` associa i numeri di titolo ai movie object. — [MKVToolNix forum, mbunkus reply](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)
- MakeMKV a volte scrive "Title 00801.mpls is equal to title 00800.mpls and was skipped": riconosce i duplicati delle varianti di lingua con la logica di TITLES_FILTER_DUP_CLIP. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=138401)

#### Deduzioni
- L'uso della serie 00800 per le varianti di lingua da parte di Disney può essere una convenzione interna adottata per i dischi Disney/Marvel/Pixar più che uno standard diffuso nel settore.

#### Lacune
- Non si è trovata alcuna specifica o guida della BDA che dia un significato agli intervalli di numeri delle playlist. L'unica fonte autorevole per le playlist di un disco è `MovieObject.bdmv` / `index.bdmv`.

---

### Q4: In che cosa differisce strutturalmente una playlist "play all" da quella di un singolo episodio (numero di PlayItem, nomi delle clip, durata)?

#### In breve
Una playlist "play-all" concatena le clip di più episodi come PlayItem consecutivi, mentre la playlist di un singolo episodio ha di solito un PlayItem che punta a una clip M2TS. La differenza strutturale sta nel numero di PlayItem e nella durata totale.

#### Risultati con fonte
- Su un disco di serie TV, una playlist (.mpls) "PLAY ALL" dà un solo titolo con tutti gli episodi in sequenza; al suo interno dovrebbero esserci i capitoli di ogni episodio. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123032)
- Sul tipico disco di una serie, le playlist dei singoli episodi hanno una segment map di una clip ("2" significa la clip dell'episodio 2 = 00002.m2ts). La segment map di una play-all elenca in ordine tutte le clip degli episodi ("1,2,3,4,5" per cinque episodi). È così che programmi come MakeMKV mostrano la differenza. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- I numeri delle clip nella segment map non seguono sempre l'ordine degli episodi. Una playlist "PLAY ALL" aiuta a ricostruire l'ordine giusto, che le playlist dei singoli episodi da sole possono non rendere evidente. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- Su un disco della stagione 1 di Westworld la playlist 105 veniva prima della 107, che veniva prima della 103: conferma che i numeri degli MPLS non corrispondono all'ordine degli episodi. — [MKVToolNix forum](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)
- Alcuni dischi mettono tutti gli episodi in una sola clip con marker di capitolo fra l'uno e l'altro (senza playlist separate per episodio). Altri affidano il play-all a codice Java, che programmi come MakeMKV non vedono direttamente. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- Le playlist Blu-ray possono richiamare le stesse clip senza duplicare i dati (a differenza dei DVD), quindi una play-all riusa gli stessi file M2TS già richiamati dalle playlist dei singoli episodi. — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=144217)

#### Deduzioni
- Poiché negli MPLS non c'è un campo sul tipo di contenuto, l'unico modo affidabile per distinguere via codice una play-all dalla playlist di un episodio è: (a) confrontare il numero di PlayItem, (b) confrontare la durata totale con la durata nota di un episodio, oppure (c) incrociare con `MovieObject.bdmv` per vedere come `index.bdmv` etichetta il titolo.

#### Lacune
- Nessuna fonte ha confermato dal testo della specifica la struttura esatta di un PlayItem MPLS (campi: nome della clip, in_time, out_time, still_mode); è nota dal codice di libbluray, ma il sorgente grezzo non è stato recuperato in questa sessione.

---

### Q5: Il file CLPI (clip info) ha qualche flag che identifichi il tipo di contenuto?

#### In breve
Il CLPI ha un byte `clip_stream_type` e un byte `application_type` nel blocco ClipInfo. I valori di `application_type` (da 1 a 7) distinguono le clip del film nel main path dagli stream dei sub path (menu, sottotitoli testuali, ecc.), ma sono flag per clip, non per playlist.

#### Risultati con fonte
- La struct `CLPI_CLIP_INFO` di libbluray contiene sia `clip_stream_type` sia `application_type` come interi senza segno a 8 bit. Lo strumento `clpi_dump.c` li stampa entrambi, cercando `application_type` in una tabella di nomi. — [libbluray clpi_data.h (centos mirror)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)
- I valori di `application_type` da 1 a 7 etichettano le clip come transport stream del main path o di un sub path, e coprono fra l'altro main path del film, slideshow, menu e sottotitoli testuali. Il valore 1 è il transport stream principale del main path di un film; il 6 è un transport stream secondario per sottotitoli testuali. — [web search result summarizing clpi_dump.c](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/devtools/clpi_dump.c)
- Il parser di ClipInfo in `clpi_parse.c` legge `clip_stream_type` e `application_type` come due valori a 8 bit consecutivi, seguiti da 31 bit riservati e da un flag ATC delta. — [libbluray clpi_data.h via centos mirror (search result snippet)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)
- `program_info` del CLPI contiene il numero di program sequence, l'indirizzo di partenza di ciascuna e i PID dei pacchetti del transport stream. Ogni voce di program stream ha: `pid`, `coding_type`, `format`, `rate`, `aspect`, `oc_flag`, `char_code`, `lang`. — [libbluray clpi_data.h (centos mirror)](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)

#### Deduzioni
- Una clip con `application_type` = 1 (main path, film) è l'unico indizio a livello di CLPI che una clip sia "contenuto principale". Ma non distingue comunque se la clip è un film, un episodio TV o un extra: tutti userebbero `application_type` = 1.

#### Lacune
- I valori interi esatti di ogni etichetta di `application_type` (la tabella completa di `clpi_dump.c`) non sono stati verificati direttamente perché il file sorgente ha risposto 404 più volte. Negli estratti di ricerca comparivano solo i valori 1 (main path del film) e 6 (sub path di sottotitoli testuali).

---

### Q6: Esistono fonti pubbliche sulla specifica Blu-ray o riferimenti di reverse engineering ben documentati?

#### In breve
La specifica della BDA non è pubblica. Le migliori fonti pubbliche sono la pagina Wikibooks User:Bdinfo/mpls, il codice sorgente di libbluray (GitLab di VideoLAN) e il repository lw/BluRay citato dai parser MPLS in Rust e Python.

#### Risultati con fonte
- La specifica ufficiale della Blu-ray Disc Association (BDA) (BD-ROM Part 3) non è pubblicata; per accedervi serve essere membri della BDA. — [docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)
- Il riferimento principale di reverse engineering è la pagina Wikibooks [https://en.wikibooks.org/wiki/User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls), che descrive l'intestazione MPLS e gli offset dei byte di AppInfoPlayList.
- libbluray (la principale implementazione open source) sta su [code.videolan.org/videolan/libbluray](https://code.videolan.org/videolan/libbluray). File sorgente rilevanti: `src/libbluray/bdnav/mpls_parse.c`, `src/libbluray/bdnav/mpls_data.h`, `src/libbluray/bdnav/navigation.c`, `src/libbluray/bdnav/clpi_data.h`, `src/libbluray/bdnav/clpi_parse.c`, `src/libbluray/bluray.h`. La documentazione doxygen dell'API pubblica è su [videolan.videolan.me/libbluray/](https://videolan.videolan.me/libbluray/bluray_8h_source.html).
- Il crate Rust `mpls` (docs.rs/mpls) e il pacchetto Python `pyparsebluray` (pypi.org/project/pyparsebluray) citano entrambi la "lw/BluRay MPLS wiki" come fonte principale; è un repository GitHub separato (non trovato direttamente in questa sessione). — [docs.rs mpls](https://docs.rs/mpls/latest/mpls); [pypi pyparsebluray](https://pypi.org/project/pyparsebluray/0.1.2)
- BDedit è un programma con interfaccia grafica che apre e mostra i file `index.bdmv`, `MovieObject.bdmv`, `.mpls`, `.clpi` e `.bdjo`, utile per ispezionare la struttura di dischi reali. — [Scribd BDedit doc](https://www.scribd.com/document/952826306/BDedit)
- Il forum VideoHelp e il forum Doom9 (doom9.org) sono riferimenti della comunità, ma il forum Doom9 non era raggiungibile in questa sessione (connessione rifiutata).

#### Lacune
- L'URL del repository GitHub lw/BluRay non è stato confermato (diversi parser lo citano ma non si è trovato un URL diretto).
- Wiki e forum di Doom9 non erano raggiungibili durante questa ricerca.

---

### Q7: Che cosa fa esattamente il flag TITLES_RELEVANT di libbluray? Con quali criteri filtra i titoli?

#### In breve
`TITLES_RELEVANT` (= 0x03) è l'OR bit a bit di `TITLES_FILTER_DUP_TITLE` (0x01) e `TITLES_FILTER_DUP_CLIP` (0x02). Dice a `bd_get_titles()` di togliere le playlist che sono duplicati strutturali di altre e quelle con segmenti di clip ripetuti troppe volte. Il filtro sta in `navigation.c`, non nel parser MPLS.

#### Risultati con fonte
- Definizioni delle macro in `bluray.h`: `TITLES_ALL = 0`, `TITLES_FILTER_DUP_TITLE = 0x01`, `TITLES_FILTER_DUP_CLIP = 0x02`, `TITLES_RELEVANT = 0x03`. — [libbluray bluray.h doxygen source](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- `_filter_dup` in `navigation.c` controlla se una playlist candidata è un duplicato strutturale di una già accettata. Il controllo di base confronta `list_count` (numero di PlayItem) e, per ogni elemento, `in_time` / `out_time`. — [libbluray navigation.c centos mirror](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- Una patch di HandBrake del 2014 proponeva di estendere `_filter_dup` al confronto della tabella degli stream: numero di stream video, audio, PG, IG, audio secondario e video secondario, poi gli attributi di ogni stream (stream_type, coding_type, pid, subpath_id, subclip_id, format, rate, char_code, lang). Una nuova funzione `_stream_cmp` fa il confronto per stream. — [libbluray-devel mailing list 2014-May](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001457.html)
- `_find_repeats` conta quanti PlayItem di una playlist puntano alla stessa clip con gli stessi in_time e out_time di un elemento dato. `_filter_repeats` scarta una playlist quando il conteggio di un elemento supera la soglia configurata. — [libbluray navigation.c via search result snippet](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- La logica del filtro sulle ripetizioni è stata copiata da `mpls_dump.c` in un commit del 2010 intitolato "filter out titles with repeating clips." — [web search result citing libbluray commit history](https://code.videolan.org/videolan/libbluray/-/blob/7cd4a904ee2589586e6dc3d57944a3377e093310/src/libbdnav/navigation.c)
- `stream_bluray.c` di mpv chiama `bd_get_titles(bd, TITLES_RELEVANT, angle)` quando elenca i titoli disponibili. — [mpv stream_bluray.c (tjdev mirror)](https://git.tjdev.de/mirror/mpv/src/commit/408b7eecee2283a6e373cff03bb3156bd721632f/stream/stream_bluray.c)
- Lo scopo del filtro combinato è far emergere i titoli "utili": le vere playlist di contenuto, senza le tante playlist quasi duplicate che alcuni studi (per esempio Lionsgate) mettono sui dischi per offuscarli. — [web search result summarizing libbluray-devel filter-dup discussion](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)

#### Deduzioni
- TITLES_RELEVANT toglie soprattutto due categorie: (1) i cloni per lingua o configurazione che differiscono solo per alcune clip sostituite (presi da `_filter_dup`) e (2) le playlist in loop o da menu che ripetono molte volte lo stesso breve segmento (prese da `_filter_repeats`). Quindi può scartare per errore una playlist legittima se la sua struttura di clip coincide con quella di un'altra.

#### Lacune
- Non è confermato se la patch di HandBrake del 2014 sia poi entrata in libbluray upstream. L'attuale `navigation.c` upstream (code.videolan.org) non è stato recuperato per restrizioni di accesso.
- Il valore esatto della soglia di ripetizioni usata da `_filter_repeats` non si è trovato in nessuna fonte recuperabile.

---

## Scelta dei titoli in libbluray e MakeMKV

### Che cosa fa il flag TITLES_RELEVANT di libbluray?

#### In breve
`TITLES_RELEVANT` unisce due flag di una bitmask (`TITLES_FILTER_DUP_TITLE | TITLES_FILTER_DUP_CLIP`) e viene passato a `bd_get_titles()` per scartare le playlist con la stessa struttura di altre già accettate; il terzo argomento, `min_title_length`, è una soglia di durata minima separata, in secondi.

#### Risultati con fonte
- Definizioni dei flag in `src/libbluray/bluray.h`: `TITLES_ALL = 0`, `TITLES_FILTER_DUP_TITLE = 0x01` (toglie i titoli duplicati), `TITLES_FILTER_DUP_CLIP = 0x02` (toglie i titoli con clip duplicate), `TITLES_RELEVANT = (TITLES_FILTER_DUP_TITLE | TITLES_FILTER_DUP_CLIP)` — [VideoLAN doxygen (redirected)](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- Firma di `bd_get_titles`: `BD_PUBLIC uint32_t bd_get_titles(BLURAY *bd, uint8_t flags, uint32_t min_title_length);`: il terzo argomento è una durata sotto la quale i titoli vengono esclusi — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- `libavformat/bluray.c` di FFmpeg chiama `bd_get_titles(bd->bd, TITLES_RELEVANT, MIN_PLAYLIST_LENGTH)` con `MIN_PLAYLIST_LENGTH` definito a 180 ("3 min"), poi sceglie la playlist con la `info->duration` più lunga — [HuggingFace/camenduru FFmpeg mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)
- Un commit di VLC del 2010 usava `TITLES_RELEVANT` più un ciclo sulla durata maggiore per scegliere il titolo principale — [vlc-commits mailing list](https://mailman.videolan.org/pipermail/vlc-commits/2010-October/003757.html)
- Il filtro interno di `navigation.c` usa `_filter_dup()` per confrontare le playlist per list_count, nome della clip e tempi di in/out. Una patch proposta da HandBrake nel 2014 aggiungeva anche il confronto a livello di stream (tipo di stream, tipo di codifica, PID, lingua, numero di stream) per ogni play item — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- `_filter_repeats` / `_find_repeats` in `navigation.c` scartano i titoli che riusano la stessa clip con gli stessi tempi di in/out in più play item; il commento nel codice dice "Ignore titles with repeated segments" — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html); [CentOS git mirror (navigation.c)](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/navigation.h)
- `nav_get_title_list` in `navigation.c` legge la cartella `BDMV/PLAYLIST` e costruisce l'elenco dei titoli nell'ordine del filesystem — [CentOS git mirror](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/navigation.h)
- L'indice dei titoli restituito da `bd_get_titles` dipende dall'ordine: "the playlist is processed in the order that they are read from the PLAYLIST directory" — [HandBrake PR #5985 discussion](https://github.com/HandBrake/HandBrake/pull/5985)

#### Deduzioni
- `TITLES_FILTER_DUP_CLIP` è il flag chiave per riconoscere le playlist "play-all": una play-all che richiama una volta la clip di ogni episodio fa comparire quelle clip anche nelle playlist dei singoli episodi, quindi quelle clip sono "clip duplicate". Dalle fonti disponibili però non è certa la direzione del filtro: non è chiaro se venga scartata prima la play-all o la playlist dell'episodio.
- Il percorso `_filter_repeats` riguarda proprio una playlist che riusa la stessa clip al suo interno (cioè una clip compare due volte nella stessa playlist), forma diversa dal caso della play-all.
- Lo stato della patch del 2014 a livello di stream (entrata o no) non risulta da queste fonti; il comportamento può cambiare fra versioni di libbluray.

#### Lacune
- Il corpo di `_filter_dup` e `_filter_repeats` nell'attuale libbluray upstream (diverso dalla versione con la patch del 2014) non è stato recuperato perché code.videolan.org ha risposto con un blocco anti-bot Anubis, e non si è trovato un mirror leggibile del sorgente attuale.
- Non si sa se la patch del 2014 sul confronto degli stream sia entrata upstream o resti solo in fork a valle (Nixpkgs, LAV).


### libbluray offre una scelta del "titolo principale" oltre al filtro sulla durata?

#### In breve
Non esiste un'API dedicata al "titolo principale"; chi vuole il film principale deve scorrere l'elenco filtrato da `bd_get_titles()` e applicare una propria euristica (di solito la durata maggiore).

#### Risultati con fonte
- `bd_get_titles()` restituisce un conteggio; i chiamanti usano `bd_get_title_info(bd, i, 0)` in un ciclo per leggere i metadati di ogni titolo (numero di playlist, durata, numero di clip, capitoli). Nessuna funzione restituisce da sola "il titolo principale" — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)
- `bluray.c` di FFmpeg sceglie dall'insieme filtrato la playlist con la `info->duration` più lunga quando non è stata indicata una playlist — [HuggingFace/camenduru FFmpeg mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)
- mpv salva il conteggio di `TITLES_RELEVANT` e lo espone come proprietà "titles"; non espone un indice del titolo "principale" — [mpv git mirror](https://git.quad4.io/Mirrors/mpv/commit/c9a740fccd1128e43542beeec6f50bc59c2bddb9)
- `bd_get_disc_info()` restituisce metadati del disco, fra cui i campi `first_play_supported` e `top_menu_supported`, che però non indicano direttamente la playlist del film principale — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)

#### Deduzioni
- Lo schema abituale (FFmpeg, VLC, HandBrake) è: chiamare `bd_get_titles(TITLES_RELEVANT, min_len)`, poi scegliere dal risultato il titolo più lungo.
- Sui dischi di serie TV questo schema non funziona, perché ci sono più playlist di episodi di durata uguale o simile; i chiamanti devono mostrare all'utente tutti i titoli filtrati.

#### Lacune
- Dalle fonti disponibili non è confermato se `bd_get_disc_info()` restituisca un identificativo di "playlist di primo livello" utilizzabile come indizio del film principale.


### Come sceglie MakeMKV quali titoli mostrare come voci "principali"?

#### In breve
MakeMKV è closed source (la parte binaria è proprietaria); per la scelta dei titoli usa il riconoscimento delle "playlist finte" via Java sui dischi offuscati e un controllo di identità della segment map per i duplicati. Dai forum risulta che per le voci scartate scrive nel log "title N is the same as title M, and was therefore skipped".

#### Risultati con fonte
- Il log di MakeMKV scrive un messaggio del tipo "title 6 is the same as title 7, and was therefore skipped" quando scarta una playlist — [MakeMKV forum, Feature Request – Show Duplicate Titles](https://forum.makemkv.com/forum/viewtopic.php?p=52406)
- Sui dischi con playlist offuscate via Java, MakeMKV usa un Java Runtime Environment per riconoscere le playlist finte e marca il film principale trovato con "(FPL_MainFeature)" nell'elenco dei titoli — [MakeMKV forum, detect fake playlists](https://forum.makemkv.com/forum/viewtopic.php?p=95219)
- La versione 1.16.4 (2021) ha portato "much better support for discs with Java playlist obfuscation" — [VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)
- Per file .mpls identici byte per byte, il log di MakeMKV dice che sono identici e uno viene scartato senza avviso; un rimedio degli utenti è cancellare il .mpls scelto da un backup decifrato, così MakeMKV ripiega sull'altro — [MakeMKV forum, Grand Budapest Hotel thread](https://forum.makemkv.com/forum/viewtopic.php?p=76586)
- Sui dischi multi-angolo (per esempio Grand Budapest Hotel), MakeMKV elenca la stessa playlist una volta per angolo; una seconda playlist con un numero diverso ma contenuto simile viene saltata come "equal" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=78098)
- Gli utenti descrivono un duplicato come due segment map identiche (stessi .m2ts richiamati); la "segment map" visibile nell'interfaccia di MakeMKV è l'elenco delle clip .m2ts a cui punta una playlist — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=77981)

#### Deduzioni
- Il controllo dei duplicati di MakeMKV senza Java sembra confrontare l'identità dei segmenti (quali clip .m2ts compaiono nella playlist e in che ordine), come il confronto dei nomi delle clip di `_filter_dup` in libbluray.
- Il percorso FPL_MainFeature per i dischi Java è un'analisi separata e più sofisticata, che parte solo quando Java è disponibile.

#### Lacune
- La parte binaria di MakeMKV è proprietaria; l'algoritmo esatto (se confronti durata, elenco delle clip, metadati degli stream o tutti e tre) non è documentato pubblicamente.
- Non è confermato se MakeMKV filtri con una `min_title_length` prima di mostrare l'elenco dei titoli.
- Le note di rilascio delle versioni 1.17.x (2023–2024) non citano modifiche alla logica di scelta dei titoli — [VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)


### Su un Blu-ray di una serie TV, MakeMKV mostra tutte le playlist o solo quelle principali?

#### In breve
MakeMKV mostra tutte le playlist che passano il suo filtro dei duplicati; gli episodi compaiono come titoli separati accanto all'eventuale play-all. Le playlist per lingua (per esempio 00800 e 00801) possono essere filtrate o no, secondo se MakeMKV le considera identiche.

#### Risultati con fonte
- Su un disco di serie TV gli episodi compaiono come titoli separati, riconoscibili da durata e numero di capitoli. Non c'è un'etichetta automatica degli episodi; l'utente deve confrontare le durate — [MakeMKV forum, How to tell which title is which episode](https://forum.makemkv.com/forum/viewtopic.php?p=7382)
- Gli utenti riferiscono che gli episodi seguono in genere l'ordine dei nomi dei file .mpls o .m2ts, il che aiuta con episodi della stessa durata — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122607)
- Il titolo "play-all" si riconosce dal numero alto di capitoli e dalle dimensioni grandi; gli utenti lo escludono a mano quando estraggono i singoli episodi — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122116)
- Quando un disco usa la protezione a "fake Playlist", MakeMKV può riconoscere solo gli episodi 1 e 2 su un disco da 4, oppure mostrare 300 titoli identici — [MakeMKV forum, Mr. Selfridge S4 thread](https://forum.makemkv.com/forum/viewtopic.php?p=60109)
- Sui dischi di film, le playlist per lingua (00800 inglese, 00801 spagnolo/francese o giapponese) differiscono solo nei cartelli dei titoli di testa e nei titoli di coda; a volte MakeMKV le mostra entrambe — [MakeMKV forum Redbox thread](https://forum.makemkv.com/forum/viewtopic.php?p=68872); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68915)
- I dischi Disney e Fox seguono uno schema in cui 00800.mpls è la versione inglese e le altre sono alternative; il comportamento esatto del filtro dipende dalle differenze nei metadati degli stream — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68914)
- Un utente del forum ha osservato che su un disco MakeMKV ha filtrato gli episodi 3–5 come "the same as in title 1", fondendoli per errore — [MakeMKV forum, Feature Request – Show Duplicate Titles](https://forum.makemkv.com/forum/viewtopic.php?p=52373)

#### Deduzioni
- Lo schema di playlist alternative 01xxx contro 00xxx per le serie TV non si è trovato nelle fonti consultate; lo schema 00800/00801 è confermato solo per i dischi di film.
- Il filtro dei duplicati di MakeMKV può dare falsi positivi sulle serie TV, fondendo episodi distinti se la loro struttura di clip coincide.

#### Lacune
- Nessuna fonte conferma come MakeMKV tratti in particolare l'intervallo di MPLS 01xxx sui dischi di serie TV rispetto alle playlist principali 00xxx.
- Nessuna fonte trovata documenta se MakeMKV usi il numero di clip (numero di PlayItem) come segnale per distinguere play-all ed episodio.


### libbluray usa il numero di PlayItem (clip) per riconoscere le playlist play-all?

#### In breve
`TITLES_FILTER_DUP_CLIP` e `_filter_repeats` affrontano insieme il problema della play-all in modo indiretto: il primo scarta i titoli che condividono clip con un altro titolo già accettato, il secondo quelli che riusano la stessa clip al proprio interno; nessuno dei due usa il numero di clip come criterio diretto.

#### Risultati con fonte
- `_filter_repeats` cerca nella stessa playlist play item che puntano alla stessa clip con gli stessi tempi di in/out e toglie quei titoli; commento: "Ignore titles with repeated segments" — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- La struct `BLURAY_TITLE_INFO` contiene il numero di clip insieme alla durata e ai capitoli — [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)
- La discussione della PR di HandBrake (2024) nota che `TITLES_FILTER_DUP_CLIP` toglie i titoli con clip duplicate indipendentemente da quale titolo sia stato visto prima — [HandBrake PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)
- La pagina man di `bd_list_titles` (Debian 12) documenta `min_title_length` come durata minima, ma non elenca un parametro "numero minimo di clip" — [DOKK manpages](https://dokk.org/manpages/debian/12/libbluray-bin/bd_list_titles.1.en)

#### Deduzioni
- La play-all di un disco con 4 episodi richiamerebbe una volta sola ciascuna delle 4 clip degli episodi. Le stesse clip compaiono anche nelle playlist dei singoli episodi. `TITLES_FILTER_DUP_CLIP` toglierebbe la play-all (oppure i singoli episodi) secondo l'ordine del filesystem.
- Il percorso `_filter_repeats` serve proprio contro i trucchi a livello di disco (per esempio un'unica grande playlist con tutti gli episodi intercalati che richiama due volte certe clip).
- Nessuno dei due meccanismi etichetta una playlist come "play-all"; l'effetto è strutturale, non semantico.

#### Lacune
- Il comportamento esatto di `TITLES_FILTER_DUP_CLIP` quando ci sono sia una play-all sia le playlist dei singoli episodi (quale viene scartata: la play-all o gli episodi?) non è stato confermato senza leggere il corpo attuale di `navigation.c`.


### Qual è l'algoritmo di riconoscimento dei titoli "duplicati" di libbluray e MakeMKV?

#### In breve
`_filter_dup()` di libbluray fa un confronto strutturale degli elenchi di play item degli MPLS (nome della clip + tempi di in/out); una patch del 2014 proponeva di aggiungere il confronto degli attributi degli stream per ogni play item, per ridurre i falsi positivi. MakeMKV usa un controllo di identità separato (closed source) basato sulle segment map.

#### Risultati con fonte
- Il `_filter_dup` di base in `navigation.c` confronta `pl->list_count` (numero di play item) e, per ogni play item, nome della clip, `in_time`, `out_time`. Se tutto coincide, la playlist è un duplicato — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- La patch a `_filter_dup` proposta da HandBrake nel 2014 aggiungeva: per ogni play item il numero di stream per tipo (`num_video`, `num_audio`, `num_pg`, `num_ig`, `num_secondary_audio`, `num_secondary_video`) e il confronto campo per campo di ogni stream (`stream_type`, `coding_type`, `pid`, `subpath_id`, `subclip_id`, `format`, `rate`, `char_code`, `lang[4]`) — [libbluray-devel mailing list](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- La patch risulta inclusa in Nixpkgs ("Make it smarter about filtering duplicates") — [alioth.systems nixpkgs mirror](https://git.alioth.systems/mirrors/nixpkgs/commit/66216ea6db71c59505b1144432233f6fbb1d1561)
- MakeMKV elimina i duplicati in base all'identità della segment map; utenti e messaggio di log di MakeMKV usano "same" nel senso che la sequenza di clip .m2ts è identica — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=52406); [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=77981)

#### Deduzioni
- Prima della patch del 2014, una playlist con le stesse clip ma una traccia audio in un'altra lingua (PID o codici lingua diversi) passava `_filter_dup` come diversa, coerente con la conservazione delle playlist per lingua.
- Dopo la patch del 2014, quelle playlist per lingua verrebbero trattate come duplicati e scartate, il che potrebbe spiegare l'eccesso di filtro di MakeMKV su alcuni dischi.

#### Lacune
- Non si sa se la patch del 2014 sul confronto degli stream sia mai entrata nel repository ufficiale di libbluray (e non solo in Nixpkgs).
- Non è confermato se l'attuale `_filter_dup` di libbluray upstream usi il confronto di base o quello con la patch.


### HandBrake usa libbluray o un proprio parser MPLS?

#### In breve
HandBrake usa libbluray direttamente, tramite il suo wrapper `libhb/bd.c`; non ha un parser MPLS separato. Il wrapper chiama `bd_get_titles()` con `TITLES_FILTER_DUP_CLIP` (sempre) più `TITLES_FILTER_DUP_TITLE` (a meno che l'utente non attivi "keep duplicate titles"), poi ordina i risultati per numero di MPLS.

#### Risultati con fonte
- `libhb/bd.c` di HandBrake (il gestore dei Blu-ray, file presente nel repository) include `libbluray/bluray.h` e chiama `bd_open()`, `bd_get_titles()`, `bd_get_title_info()`, `bd_select_playlist()` e `bd_select_angle()` — [HandBrake bd.c, letto tramite l'API di GitHub da HandBrake/HandBrake master]
- I flag usati: `uint8_t flags = TITLES_FILTER_DUP_CLIP; if (!keep_duplicate_titles) { flags |= TITLES_FILTER_DUP_TITLE; }`; `min_title_length` vale 0 (nessuna durata minima) — [HandBrake bd.c, letto tramite l'API di GitHub]
- Dopo la scansione, `qsort(d->title_info, d->title_count, sizeof(BLURAY_TITLE_INFO*), title_info_compare_mpls)` ordina i titoli per numero di MPLS — [HandBrake bd.c, letto tramite l'API di GitHub]
- La PR #5985 di HandBrake (unita il 29 luglio 2024, milestone 1.9.0) ha aggiunto l'opzione CLI `--keep-duplicate-titles` e le relative preferenze grafiche per Mac, Linux e Windows, per esporre l'interruttore `TITLES_FILTER_DUP_TITLE` — [HandBrake PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)
- `hb_bd_init()` accetta un parametro intero `keep_duplicate_titles` salvato in `hb_bd_t` — [HandBrake bd.c, letto tramite l'API di GitHub]
- Quando libbluray non riesce ad aprire il disco (`bd == NULL`), HandBrake scrive "bd: not a bd - trying as a stream/file instead" e ripiega sulla gestione DVD/stream — [HandBrake bd.c, letto tramite l'API di GitHub]; confermato da segnalazioni di utenti sul [HandBrake forum](https://forum.handbrake.fr/viewtopic.php?p=108684)

#### Deduzioni
- HandBrake non sceglie mai da solo un "titolo principale"; scansiona tutti i titoli filtrati, li ordina per numero di MPLS e mostra l'elenco all'utente. Il titolo lo sceglie l'utente o chi chiama la CLI.
- Con `min_title_length=0` HandBrake mostra anche playlist molto brevi (trailer, menu) se passano il filtro dei duplicati.
- L'ordinamento per MPLS fa sì che il titolo 0 in HandBrake corrisponda al file .mpls con il numero più basso, non al più lungo né al primo trovato.

#### Lacune
- Il corpo della funzione di confronto `title_info_compare_mpls` (ordine MPLS crescente o decrescente) non è stato recuperato.
- Non si è studiato il comportamento di HandBrake quando libbluray restituisce 0 titoli ma esiste un `BDMV/index.bdmv` (per esempio un disco cifrato).

---

## Programmi per Blu-ray e casi limite: come trattano le playlist i programmi per utenti e appassionati

### VLC + libbluray: come vengono presentati i titoli

#### In breve
VLC si affida interamente a libbluray per elencare i titoli. libbluray passa a VLC tutti i titoli non filtrati; VLC non aggiunge un proprio livello di filtro. L'insieme mostrato dipende da ciò che `bd_get_titles()` di libbluray restituisce dopo i suoi filtri su duplicati e durata. Senza supporto BD-J (Java) i menu vengono saltati e la riproduzione ripiega sul titolo principale scelto da libbluray.

#### Risultati con fonte
- Il log dei titoli Blu-ray di VLC riporta conteggi per tipo, per esempio `HDMV Titles: 19, BD-J Titles: 0, Other: 0`; sono diagnostici, non un filtro configurabile dall'utente — [VideoLAN forum thread](https://forum.videolan.org/viewtopic.php?p=484741)
- Quando i menu BD-J non sono supportati, libbluray scrive `BD-J menus not supported` e poi `Playing without menus`; un disco con `BD-J Titles: 79` mostrava molti titoli saltati per la mancanza di Java — [Ubuntu 18.04 VLC/Blu-ray forum thread](https://forum.videolan.org/viewtopic.php?p=493255)
- Nella comunità i numeri di titolo di VLC sono usati come riferimento, per esempio "the title VLC plays is #26", e confrontati con gli elenchi di titoli di MakeMKV — [MakeMKV forum: playlist obfuscation thread](https://forum.makemkv.com/forum/viewtopic.php?p=139239)
- Per i menu, libbluray richiede anche i file jar BD-J; senza, hanno i menu solo i dischi HDMV — [MakeMKV forum: BD-J install thread](https://forum.makemkv.com/forum/viewtopic.php?p=186594)
- Per i dischi commerciali servono anche le chiavi di decifratura del disco (VUK dal keydb AACS); libbluray da sola non decifra — documentazione generale di libbluray

#### Deduzioni
- VLC mostra in pratica tutto ciò che passa i filtri di libbluray sui duplicati e sulla durata minima; per un disco con più di 300 playlist finte, VLC mostrerebbe un lungo elenco di titoli a meno che il filtro dei duplicati di libbluray non le tolga.
- Senza BD-J (la situazione tipica su Linux), VLC ripiega sull'euristica di libbluray per il titolo principale, che può non coincidere con i menu dei dischi che usano Java per il "Play All".

#### Lacune
- Non si è trovata documentazione ufficiale di VLC che indichi un numero massimo o minimo di titoli mostrati all'utente.
- Un eventuale filtro di VLC fra l'elenco dei titoli di libbluray e quello dell'interfaccia non è stato confermato né nel sorgente pubblico né nella documentazione.


### HandBrake: euristiche di scansione dei Blu-ray

#### In breve
HandBrake scansiona tutti i titoli sopra una durata minima configurabile (default 10 secondi), lascia interamente a libbluray il filtro dei duplicati con i flag `TITLES_FILTER_DUP_CLIP`/`TITLES_FILTER_DUP_TITLE`, poi sceglie il "film principale" con `hb_bd_main_feature()`, che prende il titolo più lungo pesato per rango del formato video. HandBrake non ha un'euristica propria per riconoscere gli episodi.

#### Risultati con fonte
- `--min-duration` di HandBrakeCLI vale di default 10 secondi; i titoli più corti non vengono scansionati; la regola vale solo per le sorgenti da disco (Blu-ray e DVD), non per i normali file video — [HandBrakeCLI man page (Debian)](https://dyn.manpages.debian.org/testing/handbrake-cli/HandBrakeCLI.1); [HandBrake 1.3.0 release notes (Neowin)](https://www.neowin.net/news/handbrake-130)
- `scan.c` di HandBrake: `hb_bd_init()` viene chiamata se il percorso di ingresso non ha un'estensione video nota; passa sempre `TITLES_FILTER_DUP_CLIP` e di default anche `TITLES_FILTER_DUP_TITLE` a libbluray, a meno che non sia impostato `keep_duplicate_titles` — [HandBrake GitHub: libhb/scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)
- `hb_bd_main_feature()` di HandBrake sceglie il titolo più lungo fra quelli lunghi almeno il 70% del più lungo visto; gli spareggi si decidono sul rango del formato video (una tabella), poi sulla durata, poi sul numero di capitoli — [HandBrake GitHub: libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- I capitoli che iniziano entro 1,5 secondi dalla fine di un titolo vengono tolti da quel titolo — [HandBrake GitHub: libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- I titoli in cui `DecodePreviews()` non trova fotogrammi decodificabili vengono tolti dai risultati dopo la scansione — [HandBrake GitHub: libhb/scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)
- Lo script di terzi AutoHandbrake aggiunge un'euristica di raggruppamento degli episodi: cerca un gruppo di titoli consecutivi di durata simile sopra una durata minima e propone di estrarli come episodi numerati — [AutoHandbrake GitHub](https://github.com/marxjohnson/AutoHandbrake)
- Un rimedio della comunità per i dischi anime (Squid Girl) è stato fare un backup decifrato con MakeMKV e poi passare a HandBrake la cartella del backup, ottenendo un unico grande file con i marker dei capitoli, da dividere in seguito — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82552)

#### Deduzioni
- Per un disco di serie TV con 4 episodi da circa 45 minuti, HandBrake mostrerà probabilmente tutti e 4 come titoli separati (più gli extra sopra i 10 secondi), perché nessun titolo è più del 70% degli altri: l'euristica del film principale non li fonde. L'utente deve comunque sapere quale numero di titolo corrisponde a quale episodio.
- Per le playlist "play all" che sono semplicemente il titolo più lungo, la scelta del film principale di HandBrake le preferisce, e può proporre la play-all come titolo predefinito da codificare.

#### Lacune
- Il valore di default esatto di `max_duration` nella scansione dei Blu-ray di HandBrake non è confermato nella documentazione pubblica.
- I valori della tabella dei ranghi dei formati video di HandBrake, e che cosa codificano (codec? risoluzione?), non sono confermati dall'estratto di sorgente visto.


### Plex: scansione dei dischi Blu-ray

#### In breve
Plex non supporta la scansione diretta di un disco Blu-ray o di una cartella BDMV per associare gli episodi. Si aspetta singoli file video già estratti, con nomi secondo la convenzione SxxExx. Se si punta Plex su una struttura BDMV, o non riconosce niente o associa i file agli episodi sbagliati.

#### Risultati con fonte
- A un utente del forum di Plex che chiedeva come riprodurre cartelle BDMV è stato risposto "No", con il consiglio di estrarre con MakeMKV — [Plex forum: BDMV play thread](https://forums.plex.tv/t/how-do-i-mak-plex-play-dbmv-folders/313948)
- La scansione di Plex si basa su convenzioni di nome dei file (SxxExx) confrontate con TheTVDB o TheMovieDB; lo scanner è "relatively flexible for movies, pretty strict for TV shows" — [Plex support](https://support.plex.tv/?p=46272)
- Un utente di Infuse + Plex ha visto comparire, puntando Plex su una BDMV, file `.m2ts` numerati invece dei film — [Infuse community thread](https://community.firecore.com/t/plex-infuse-bdmv/20496)
- Lo scanner DVD di Plex ha scambiato un DVD con 4 episodi per gli episodi 1–16, leggendo direttamente i file VOB invece di seguire la navigazione degli IFO — [Plex forum: TV DVD scanner](https://forums.plex.tv/t/tv-dvd/6074)
- I contenuti "stacked" di Plex (file in più parti) hanno dei limiti: al massimo 8 parti, e non tutti i client Plex li supportano — [Plex forum: complex naming](https://forums.plex.tv/t/complex-media-naming-organization-question/141171)
- Una nota sul forum di Jellyfin dice che "finding out what a BDMV contains can be either very hard or impossible" — [Jellyfin forum](https://forum.jellyfin.org/printthread.php?tid=3258)

#### Deduzioni
- Perché le estrazioni da BD funzionino bene in Plex, ogni episodio va estratto/rimuxato nel proprio MKV con il nome SxxExx corretto; la scelta degli MPLS va fatta dall'utente o da MakeMKV prima che Plex entri in gioco.
- Nessun media server (Plex, Emby, Jellyfin) riconosce in modo affidabile gli episodi a livello di MPLS; la responsabilità è dei programmi di estrazione.

#### Lacune
- Non si è trovata documentazione ufficiale di Plex sul supporto alle immagini di dischi Blu-ray; la risposta confermata è che immagini disco e BDMV non sono supportate.
- Il supporto BDMV di Emby (rotto nella versione 4.9.5.0 secondo un thread del forum) non è stato approfondito.


### Strutture dei Blu-ray di serie TV che creano problemi

#### In breve
Le strutture problematiche più comuni sono: (1) playlist finte (centinaia di file MPLS quasi identici fatti per scoraggiare l'estrazione), (2) playlist "play all" costruite con un unico lungo segmento invece di più segmenti della durata di un episodio con i marker dei capitoli, (3) ordine di riproduzione gestito da Java BD-J, che nasconde la sequenza degli episodi all'ispezione statica delle playlist, e (4) voci di titolo duplicate di cui solo una ha i capitoli.

#### Risultati con fonte
- Playlist finte: dischi come *John Wick* hanno più di 300 voci di titolo duplicate; anche *Laurel & Hardy Year Two* usa questo schema; "usually when there are fake playlists there are hundreds of them" — [MakeMKV forum: fake playlist help](https://forum.makemkv.com/forum/viewtopic.php?p=173579); [MakeMKV forum: fake playlist feature request](https://forum.makemkv.com/forum/viewtopic.php?p=196100)
- Il "Play All" si può realizzare in tre modi: (a) un unico MPLS che elenca in ordine tutti i segmenti degli episodi, (b) un unico segmento che contiene tutti gli episodi, con i marker dei capitoli, oppure (c) codice Java che mette in sequenza la riproduzione; solo i casi (a) e (b) si riconoscono ispezionando gli MPLS — [MakeMKV forum: segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Disco 3 del Blu-ray di *Band of Brothers*: più titoli di durata identica (4 titoli da 1:07:20, 4 da 55:42); le 4 varianti di ogni episodio si distinguono per "hidden tracks", non per la durata — [VideoHelp forum: Which Title thread](https://forum.videohelp.com/threads/350950-Which-Title)
- Blu-ray della stagione 1 di *Rick and Morty*: estrazione fallita su titoli specifici (7, 8 e 11); il titolo 11 è "a combination of all episodes in 1 large file"; disattivando i sottotitoli sulle tracce fallite l'estrazione riesce — [MakeMKV forum: Rick and Morty S1 rip errors](https://forum.makemkv.com/forum/viewtopic.php?p=56602)
- Stagione 3 di *Rick and Morty*: video presente ma senza audio — [MakeMKV forum: Rick and Morty S3](https://forum.makemkv.com/forum/viewtopic.php?p=72343)
- *Squid Girl* (anime): MakeMKV produceva frammenti invece di episodi; il rimedio è stato un backup decifrato passato a HandBrake per ottenere un unico file con i marker dei capitoli — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82552)
- Stagione 7 australiana di *Adventure Time*: solo frammenti di 2 minuti per episodio; la struttura normale è un'unica traccia con tutti gli episodi — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82555)
- Disco 3 della stagione 1 di *Transformers Prime*: manca un episodio; il log di debug mostrava la playlist relativa saltata come duplicato — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63515)
- Disco 2 della stagione 15 di *South Park*: l'episodio 4 non viene mai riconosciuto, con più versioni di MakeMKV — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63510)
- Cofanetto Blu-ray di *Star Trek: TNG*: difetti di stampa casuali rendono illeggibili singoli episodi su certi dischi — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63507)
- Stagione 2 di *Poldark*: la numerazione delle playlist non segue l'ordine degli episodi; "don't just assume that title 205 is episode 1" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Stagione 1 di Prison Break (edizione tedesca): tutti i dischi contengono le stesse tracce più volte; MakeMKV non riesce a mostrarne le differenze — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80252)

#### Deduzioni
- Le playlist duplicate con durate quasi identiche sono la tecnica anti-copia dominante; un riconoscimento basato solo sulla durata qui fallisce.
- La segment map (elenco dei PlayItem) discrimina più della durata: una play-all ha di solito N segmenti (uno per episodio), la playlist di un episodio ne ha 1.

#### Lacune
- Non si è trovata documentazione specifica sulla struttura del Blu-ray di *Cowboy Bebop* (i problemi segnalati erano di leggibilità del disco/graffi, non di struttura MPLS).
- Non si è trovata documentazione specifica sulla struttura del Blu-ray di *The Prisoner* nelle fonti della comunità dell'estrazione.
- Non ci sono segnalazioni confermate della comunità sul Blu-ray di *Rick and Morty* che dicano se il problema dipende dalla struttura delle playlist o da difetti di masterizzazione del disco.


### Il numero di PlayItem come criterio: play-all o episodio singolo

#### In breve
Il numero di segmenti/PlayItem è un'euristica affidabile ma non sufficiente: una play-all elenca di solito N clip (una per episodio) nella sua segment map, la playlist di un episodio 1 o 2. Esistono però tre casi limite importanti: (1) i dischi con tutti gli episodi in un solo segmento usano 1 segmento per la play-all, riconoscibile solo da durata + numero di capitoli; (2) alcuni dischi gonfiano i file MPLS per nascondere la vera durata; (3) le play-all gestite da Java non hanno alcun MPLS corrispondente.

#### Risultati con fonte
- Euristica confermata dalla comunità di MakeMKV: "If there's five episodes on each disc, you may see a segment map like '1,2,3,4,5'...if you also see a .mpls title with a segment map of '1' then you know that is the first episode by itself" — [MakeMKV forum: segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- L'MPLS della play-all è anche il modo migliore per ricavare l'ordine degli episodi, perché i segmenti degli episodi non sono sempre numerati in sequenza — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53383)
- Play-all in un solo segmento: "Some have single segment with all the episodes crammed into it, hopefully with chapter markers in between" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53127)
- Play-all BD-J: "Presumably others have a bit of Java code that provides the play-all functionality. In this case, MakeMKV doesn't really have a way to show that to you" — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- Riempimento: "Some Blu-rays are authored with a bazillion tracks of the same movie...the player runs a small Java program to determine which track is the right one" — [MakeMKV forum: segment map thread](https://forum.makemkv.com/forum/viewtopic.php?p=53127)
- Se un titolo compare due volte, una con i capitoli e una senza, va preferita la versione con i capitoli — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=136962)
- `bluback` (una libreria Rust per estrarre Blu-ray) usa una durata minima delle playlist configurabile (default: 900 secondi) per riconoscere gli episodi, insieme all'estrazione dei marker dei capitoli — [bluback 0.7.1 crate docs](https://docs.rs/crate/bluback/0.7.1)
- Utente di MKVToolNix: una stagione di 10 episodi in cui i numeri delle playlist non seguono l'ordine degli episodi; consiglia di confrontare le durate o guardare le clip per stabilire l'associazione — [MKVToolNix help forum: segment map for TV series](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)

#### Deduzioni
- Un riconoscimento robusto dovrebbe combinare: (a) il numero di PlayItem/segmenti per distinguere playlist con più episodi da quelle con uno solo, (b) una soglia di durata minima per scartare extra e trailer, (c) il numero di capitoli come segnale secondario per il caso di tutti gli episodi in un solo segmento, e (d) l'ordine dei segmenti della play-all per ricavare la sequenza degli episodi.
- Il numero di PlayItem da solo non basta per il caso di tutti gli episodi in un solo segmento; i marker dei capitoli dentro quel segmento sono l'unico segnale strutturale.

#### Lacune
- Le fonti della comunità non citano alcun documento formale della specifica Blu-ray che confermi la struttura binaria esatta di PlayItem/PlayListMark; tutti i risultati vengono dal comportamento dei programmi e da osservazioni nei forum.
- La soglia esatta fra un extra "corto" e un episodio "vero" cambia da programma a programma: HandBrake usa di default 10 secondi, bluback 900 secondi; non c'è accordo sul valore migliore per gli anime (episodi di solito da 22-24 min) e per le serie drammatiche (42-50 min).


### Il consenso della comunità: la strategia di riconoscimento più sicura

#### In breve
Il consenso della comunità è: (1) usare BDInfo per elencare le playlist, (2) usare la segment map per trovare le playlist con un numero di segmenti pari al numero di episodi atteso, (3) incrociare con l'ordine dei segmenti della play-all per la sequenza degli episodi, (4) confermare con una breve riproduzione o confrontando i tempi dei capitoli, e (5) controllare su DVDCompare.net le associazioni specifiche di quell'edizione.

#### Risultati con fonte
- DVDCompare.net è consigliato per le informazioni specifiche di ogni edizione, comprese le associazioni playlist → episodio — [MakeMKV forum: segment map thread](https://forum.makemkv.com/forum/viewtopic.php?p=53383); [VideoHelp forum](https://forum.videohelp.com/threads/350950-Which-Title)
- BDInfo: scansionare il disco o il backup per trovare prima i due titoli più lunghi e sapere a quale file MPLS corrisponde ciascuno — [VideoHelp forum: Which Title thread](https://forum.videohelp.com/threads/350950-Which-Title)
- Per le playlist finte: clic destro sull'albero del disco in MakeMKV, deselezionare tutto, poi scegliere i titoli veri controllando le segment map — [MakeMKV forum: fake playlist help](https://forum.makemkv.com/forum/viewtopic.php?p=173579)
- La finestra INFO di MakeMKV mostra le segment map; confrontare le segment map dei singoli titoli con quella della play-all conferma quale episodio è quale — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=144217)
- Quando compaiono due voci per lo stesso titolo, tenere quella con i capitoli — [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=136962)
- I numeri delle playlist non sono sempre consecutivi né ordinati; verificare sempre — [MakeMKV forum: Poldark thread](https://forum.makemkv.com/forum/viewtopic.php?p=53072)

#### Deduzioni
- Non esiste una soluzione del tutto automatica che funzioni con tutti i dischi; per i dischi insoliti resta necessaria una verifica umana (breve riproduzione o controllo dei tempi dei capitoli).
- Per un programma automatico rivolto ai casi comuni, l'approccio con la segment map incrociato con la play-all copre la maggior parte dei Blu-ray di serie fatti bene.

#### Lacune
- Oltre a MakeMKV e BDInfo non si è trovato alcun programma con un processo automatico affidabile per riconoscere gli episodi sui Blu-ray di serie.
- `bluback` (Rust) ci prova con durata + estrazione dei capitoli, ma è poco usato e non è stato messo alla prova sui dischi problematici; la sua copertura dei casi limite non è nota.


### La logica di filtro interna di libbluray

#### In breve
`nav_find_main_title()` di libbluray sceglie la playlist più lunga che non contiene clip duplicate. Il filtro su clip duplicate e titoli duplicati è esposto con i flag dell'API (`TITLES_FILTER_DUP_CLIP`, `TITLES_FILTER_DUP_TITLE`). Un parametro di durata minima del titolo è stato aggiunto a `bd_get_titles()` nel 2011. Dal 2019 il tipo di codec (con preferenza per HEVC) è un criterio di spareggio.

#### Risultati con fonte
- Scelta originale del titolo principale: "find the playlist for the main title by picking the longest title that contains no duplicate clips" — [libbluray navigation.h, code.videolan.org](https://code.videolan.org/videolan/libbluray/-/blob/8014175c0c0a450d93d710edf836ca30b5938502/src/libbdnav/navigation.c)
- Le funzioni `_filter_dup`, `_find_repeats`, `_filter_repeats` in `navigation.c` ignorano i titoli con segmenti ripetuti, riconosciuti da ID della clip e tempi di in e out uguali — [libbluray navigation.c (CentOS snapshot)](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- Commit del 2019: aggiunto il confronto dei codec; nella scelta del titolo principale HEVC è preferito ad AVC e agli altri formati — [libbluray commit history, code.videolan.org](https://code.videolan.org/videolan/libbluray)
- Commit del 2011: aggiunto il parametro `min_title_length` a `bd_get_titles()` — [libbluray commit (code.videolan.org)](https://code.videolan.org/videolan/libbluray)
- HandBrake passa sempre `TITLES_FILTER_DUP_CLIP`; passa anche `TITLES_FILTER_DUP_TITLE` a meno che non sia impostato `keep_duplicate_titles` — [HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- `hb_bd_main_feature()` di HandBrake: un titolo è candidato se dura almeno il 70% del più lungo visto e il valore del formato video è < 8; vince il formato di rango più alto, con durata e numero di capitoli come spareggi — [HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)

#### Deduzioni
- Per i dischi con molte playlist finte che puntano alle stesse clip M2TS (contenuto con clip duplicate), `TITLES_FILTER_DUP_CLIP` dovrebbe accorciare molto l'elenco mostrato, ma solo se le playlist finte riusano letteralmente gli stessi file di clip.
- Per playlist finte che montano le stesse clip in ordine leggermente diverso, il filtro dei duplicati può non accorciare l'elenco, perché i tempi di in/out cambiano.

#### Lacune
- Il sorgente attuale esatto di `nav_find_main_title()` nell'HEAD di libbluray non è stato recuperato (il mirror CentOS ha risposto 404); i risultati vengono da uno snapshot più vecchio e dai messaggi dei commit.
- La tabella dei ranghi dei formati video in `hb_bd_main_feature()` di HandBrake non è stata esaminata; non è chiaro se dia priorità al codec, alla risoluzione o a entrambi.

## Fonti

Tutti i link citati sopra, nell'ordine in cui compaiono la prima volta.

- [Wikibooks User:Bdinfo/mpls](https://en.wikibooks.org/wiki/User:Bdinfo/mpls)
- [LAV libbluray commit](https://gitea.1f0.de/LAV/libbluray/commit/768d9c6add9b7e42651e31be055539c4ecdd8ba2)
- [docs.rs mpls crate](https://docs.rs/mpls/latest/mpls)
- [libbluray clpi_data.h](https://git.stg.centos.org/source-git/libbluray/blob/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/clpi_data.h)
- [MakeMKV forum, segment map explanation](https://forum.makemkv.com/forum/viewtopic.php?p=53072)
- [MKVToolNix forum](https://help.mkvtoolnix.download/t/segment-map-for-tv-series/1513)
- [MakeMKV forum, Rogue One thread](https://forum.makemkv.com/forum/viewtopic.php?p=55134)
- [libbluray bluray.h](https://videolan.videolan.me/libbluray/bluray_8h_source.html)
- [libbluray-devel mailing list, 2014](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001457.html)
- [libbluray commit history](https://code.videolan.org/videolan/libbluray)
- [FFmpeg bluray.c, camenduru mirror](https://huggingface.co/camenduru/ffmpeg-cuda/blob/main/libavformat/bluray.c)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=52406)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=95219)
- [VideoHelp MakeMKV version history](https://videohelp.com/software/MakeMKV/version-history)
- [PR #5985](https://github.com/HandBrake/HandBrake/pull/5985)
- [HandBrake libhb/bd.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/bd.c)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=173579)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=56602)
- [VideoHelp forum](https://forum.videohelp.com/threads/350950-Which-Title)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63515)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63510)
- [libbluray mpls_data.h via centos mirror](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/mpls_data.h)
- [BLURAY_DISC_INFO doxygen](https://videolan.videolan.me/libbluray/structBLURAY__DISC__INFO.html)
- [Web search result citing libbluray clpi_dump.c](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/devtools/clpi_dump.c)
- [fileinfo.com .MPLS extension](https://fileinfo.com/extension/mpls)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80956)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=55723)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=142684)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=138401)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123032)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=123405)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=144217)
- [pypi pyparsebluray](https://pypi.org/project/pyparsebluray/0.1.2)
- [Scribd BDedit doc](https://www.scribd.com/document/952826306/BDedit)
- [libbluray navigation.c centos mirror](https://git.stg.centos.org/source-git/libbluray/raw/4331df799a6453407bd4175499c11617e982cee7/f/src/libbluray/bdnav/navigation.c)
- [web search result citing libbluray commit history](https://code.videolan.org/videolan/libbluray/-/blob/7cd4a904ee2589586e6dc3d57944a3377e093310/src/libbdnav/navigation.c)
- [mpv stream_bluray.c (tjdev mirror)](https://git.tjdev.de/mirror/mpv/src/commit/408b7eecee2283a6e373cff03bb3156bd721632f/stream/stream_bluray.c)
- [web search result summarizing libbluray-devel filter-dup discussion](https://mailman.videolan.org/pipermail/libbluray-devel/2014-May/001458.html)
- [vlc-commits mailing list](https://mailman.videolan.org/pipermail/vlc-commits/2010-October/003757.html)
- [CentOS git mirror (navigation.c)](https://git.stg.centos.org/source-git/libbluray/blob/c8/f/src/libbluray/bdnav/navigation.h)
- [VideoLAN doxygen](https://videolan.videolan.me/libbluray/bluray_8h.html)
- [mpv git mirror](https://git.quad4.io/Mirrors/mpv/commit/c9a740fccd1128e43542beeec6f50bc59c2bddb9)
- [MakeMKV forum, Grand Budapest Hotel thread](https://forum.makemkv.com/forum/viewtopic.php?p=76586)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=78098)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=77981)
- [MakeMKV forum, How to tell which title is which episode](https://forum.makemkv.com/forum/viewtopic.php?p=7382)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122607)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=122116)
- [MakeMKV forum, Mr. Selfridge S4 thread](https://forum.makemkv.com/forum/viewtopic.php?p=60109)
- [MakeMKV forum Redbox thread](https://forum.makemkv.com/forum/viewtopic.php?p=68872)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68915)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=68914)
- [MakeMKV forum, Feature Request – Show Duplicate Titles](https://forum.makemkv.com/forum/viewtopic.php?p=52373)
- [DOKK manpages](https://dokk.org/manpages/debian/12/libbluray-bin/bd_list_titles.1.en)
- [alioth.systems nixpkgs mirror](https://git.alioth.systems/mirrors/nixpkgs/commit/66216ea6db71c59505b1144432233f6fbb1d1561)
- [HandBrake forum](https://forum.handbrake.fr/viewtopic.php?p=108684)
- [VideoLAN forum thread](https://forum.videolan.org/viewtopic.php?p=484741)
- [Ubuntu 18.04 VLC/Blu-ray forum thread](https://forum.videolan.org/viewtopic.php?p=493255)
- [MakeMKV forum: playlist obfuscation thread](https://forum.makemkv.com/forum/viewtopic.php?p=139239)
- [MakeMKV forum: BD-J install thread](https://forum.makemkv.com/forum/viewtopic.php?p=186594)
- [HandBrakeCLI man page (Debian)](https://dyn.manpages.debian.org/testing/handbrake-cli/HandBrakeCLI.1)
- [HandBrake 1.3.0 release notes (Neowin)](https://www.neowin.net/news/handbrake-130)
- [HandBrake GitHub: libhb/scan.c](https://github.com/HandBrake/HandBrake/blob/master/libhb/scan.c)
- [AutoHandbrake GitHub](https://github.com/marxjohnson/AutoHandbrake)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82552)
- [Plex forum: BDMV play thread](https://forums.plex.tv/t/how-do-i-mak-plex-play-dbmv-folders/313948)
- [Plex support](https://support.plex.tv/?p=46272)
- [Infuse community thread](https://community.firecore.com/t/plex-infuse-bdmv/20496)
- [Plex forum: TV DVD scanner](https://forums.plex.tv/t/tv-dvd/6074)
- [Plex forum: complex naming](https://forums.plex.tv/t/complex-media-naming-organization-question/141171)
- [Jellyfin forum](https://forum.jellyfin.org/printthread.php?tid=3258)
- [MakeMKV forum: fake playlist feature request](https://forum.makemkv.com/forum/viewtopic.php?p=196100)
- [MakeMKV forum: Rick and Morty S3](https://forum.makemkv.com/forum/viewtopic.php?p=72343)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=82555)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=63507)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=80252)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53383)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=53127)
- [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?p=136962)
- [bluback 0.7.1 crate docs](https://docs.rs/crate/bluback/0.7.1)
- [libbluray navigation.h, code.videolan.org](https://code.videolan.org/videolan/libbluray/-/blob/8014175c0c0a450d93d710edf836ca30b5938502/src/libbdnav/navigation.c)
