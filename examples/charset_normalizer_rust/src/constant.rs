// Ported from examples/charset_normalizer/constant.py with high fidelity.
// Exact match for all data tables. Concrete types.

use once_cell::sync::Lazy;
use std::collections::{HashMap, HashSet};

pub const TOO_SMALL_SEQUENCE: usize = 32;
pub const TOO_BIG_SEQUENCE: usize = 10_000_000;
pub const UTF8_MAXIMAL_ALLOCATION: usize = 1_112_064;

pub static ENCODING_MARKS: Lazy<HashMap<&'static str, Vec<Vec<u8>>>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert("utf_8", vec![vec![0xef, 0xbb, 0xbf]]);
    m.insert(
        "utf_7",
        vec![
            b"+/v8".to_vec(),
            b"+/v9".to_vec(),
            b"+/v+".to_vec(),
            b"+/v/".to_vec(),
        ],
    );
    m.insert("gb18030", vec![vec![0x84, 0x31, 0x95, 0x33]]);
    m.insert(
        "utf_32",
        vec![vec![0x00, 0x00, 0xfe, 0xff], vec![0xff, 0xfe, 0x00, 0x00]],
    );
    m.insert("utf_16", vec![vec![0xfe, 0xff], vec![0xff, 0xfe]]);
    m
});

pub static UNICODE_RANGES_COMBINED: Lazy<Vec<(u32, u32, &'static str)>> = Lazy::new(|| {
    vec![
        (0u32, 32u32, "Control character"),
        (32u32, 128u32, "Basic Latin"),
        (128u32, 256u32, "Latin-1 Supplement"),
        (256u32, 384u32, "Latin Extended-A"),
        (384u32, 592u32, "Latin Extended-B"),
        (592u32, 688u32, "IPA Extensions"),
        (688u32, 768u32, "Spacing Modifier Letters"),
        (768u32, 880u32, "Combining Diacritical Marks"),
        (880u32, 1024u32, "Greek and Coptic"),
        (1024u32, 1280u32, "Cyrillic"),
        (1280u32, 1328u32, "Cyrillic Supplement"),
        (1328u32, 1424u32, "Armenian"),
        (1424u32, 1536u32, "Hebrew"),
        (1536u32, 1792u32, "Arabic"),
        (1792u32, 1872u32, "Syriac"),
        (1872u32, 1920u32, "Arabic Supplement"),
        (1920u32, 1984u32, "Thaana"),
        (1984u32, 2048u32, "NKo"),
        (2048u32, 2112u32, "Samaritan"),
        (2112u32, 2144u32, "Mandaic"),
        (2144u32, 2160u32, "Syriac Supplement"),
        (2160u32, 2208u32, "Arabic Extended-B"),
        (2208u32, 2304u32, "Arabic Extended-A"),
        (2304u32, 2432u32, "Devanagari"),
        (2432u32, 2560u32, "Bengali"),
        (2560u32, 2688u32, "Gurmukhi"),
        (2688u32, 2816u32, "Gujarati"),
        (2816u32, 2944u32, "Oriya"),
        (2944u32, 3072u32, "Tamil"),
        (3072u32, 3200u32, "Telugu"),
        (3200u32, 3328u32, "Kannada"),
        (3328u32, 3456u32, "Malayalam"),
        (3456u32, 3584u32, "Sinhala"),
        (3584u32, 3712u32, "Thai"),
        (3712u32, 3840u32, "Lao"),
        (3840u32, 4096u32, "Tibetan"),
        (4096u32, 4256u32, "Myanmar"),
        (4256u32, 4352u32, "Georgian"),
        (4352u32, 4608u32, "Hangul Jamo"),
        (4608u32, 4992u32, "Ethiopic"),
        (4992u32, 5024u32, "Ethiopic Supplement"),
        (5024u32, 5120u32, "Cherokee"),
        (5120u32, 5760u32, "Unified Canadian Aboriginal Syllabics"),
        (5760u32, 5792u32, "Ogham"),
        (5792u32, 5888u32, "Runic"),
        (5888u32, 5920u32, "Tagalog"),
        (5920u32, 5952u32, "Hanunoo"),
        (5952u32, 5984u32, "Buhid"),
        (5984u32, 6016u32, "Tagbanwa"),
        (6016u32, 6144u32, "Khmer"),
        (6144u32, 6320u32, "Mongolian"),
        (
            6320u32,
            6400u32,
            "Unified Canadian Aboriginal Syllabics Extended",
        ),
        (6400u32, 6480u32, "Limbu"),
        (6480u32, 6528u32, "Tai Le"),
        (6528u32, 6624u32, "New Tai Lue"),
        (6624u32, 6656u32, "Khmer Symbols"),
        (6656u32, 6688u32, "Buginese"),
        (6688u32, 6832u32, "Tai Tham"),
        (6832u32, 6912u32, "Combining Diacritical Marks Extended"),
        (6912u32, 7040u32, "Balinese"),
        (7040u32, 7104u32, "Sundanese"),
        (7104u32, 7168u32, "Batak"),
        (7168u32, 7248u32, "Lepcha"),
        (7248u32, 7296u32, "Ol Chiki"),
        (7296u32, 7312u32, "Cyrillic Extended-C"),
        (7312u32, 7360u32, "Georgian Extended"),
        (7360u32, 7376u32, "Sundanese Supplement"),
        (7376u32, 7424u32, "Vedic Extensions"),
        (7424u32, 7552u32, "Phonetic Extensions"),
        (7552u32, 7616u32, "Phonetic Extensions Supplement"),
        (7616u32, 7680u32, "Combining Diacritical Marks Supplement"),
        (7680u32, 7936u32, "Latin Extended Additional"),
        (7936u32, 8192u32, "Greek Extended"),
        (8192u32, 8304u32, "General Punctuation"),
        (8304u32, 8352u32, "Superscripts and Subscripts"),
        (8352u32, 8400u32, "Currency Symbols"),
        (8400u32, 8448u32, "Combining Diacritical Marks for Symbols"),
        (8448u32, 8528u32, "Letterlike Symbols"),
        (8528u32, 8592u32, "Number Forms"),
        (8592u32, 8704u32, "Arrows"),
        (8704u32, 8960u32, "Mathematical Operators"),
        (8960u32, 9216u32, "Miscellaneous Technical"),
        (9216u32, 9280u32, "Control Pictures"),
        (9280u32, 9312u32, "Optical Character Recognition"),
        (9312u32, 9472u32, "Enclosed Alphanumerics"),
        (9472u32, 9600u32, "Box Drawing"),
        (9600u32, 9632u32, "Block Elements"),
        (9632u32, 9728u32, "Geometric Shapes"),
        (9728u32, 9984u32, "Miscellaneous Symbols"),
        (9984u32, 10176u32, "Dingbats"),
        (10176u32, 10224u32, "Miscellaneous Mathematical Symbols-A"),
        (10224u32, 10240u32, "Supplemental Arrows-A"),
        (10240u32, 10496u32, "Braille Patterns"),
        (10496u32, 10624u32, "Supplemental Arrows-B"),
        (10624u32, 10752u32, "Miscellaneous Mathematical Symbols-B"),
        (10752u32, 11008u32, "Supplemental Mathematical Operators"),
        (11008u32, 11264u32, "Miscellaneous Symbols and Arrows"),
        (11264u32, 11360u32, "Glagolitic"),
        (11360u32, 11392u32, "Latin Extended-C"),
        (11392u32, 11520u32, "Coptic"),
        (11520u32, 11568u32, "Georgian Supplement"),
        (11568u32, 11648u32, "Tifinagh"),
        (11648u32, 11744u32, "Ethiopic Extended"),
        (11744u32, 11776u32, "Cyrillic Extended-A"),
        (11776u32, 11904u32, "Supplemental Punctuation"),
        (11904u32, 12032u32, "CJK Radicals Supplement"),
        (12032u32, 12256u32, "Kangxi Radicals"),
        (12272u32, 12288u32, "Ideographic Description Characters"),
        (12288u32, 12352u32, "CJK Symbols and Punctuation"),
        (12352u32, 12448u32, "Hiragana"),
        (12448u32, 12544u32, "Katakana"),
        (12544u32, 12592u32, "Bopomofo"),
        (12592u32, 12688u32, "Hangul Compatibility Jamo"),
        (12688u32, 12704u32, "Kanbun"),
        (12704u32, 12736u32, "Bopomofo Extended"),
        (12736u32, 12784u32, "CJK Strokes"),
        (12784u32, 12800u32, "Katakana Phonetic Extensions"),
        (12800u32, 13056u32, "Enclosed CJK Letters and Months"),
        (13056u32, 13312u32, "CJK Compatibility"),
        (13312u32, 19904u32, "CJK Unified Ideographs Extension A"),
        (19904u32, 19968u32, "Yijing Hexagram Symbols"),
        (19968u32, 40960u32, "CJK Unified Ideographs"),
        (40960u32, 42128u32, "Yi Syllables"),
        (42128u32, 42192u32, "Yi Radicals"),
        (42192u32, 42240u32, "Lisu"),
        (42240u32, 42560u32, "Vai"),
        (42560u32, 42656u32, "Cyrillic Extended-B"),
        (42656u32, 42752u32, "Bamum"),
        (42752u32, 42784u32, "Modifier Tone Letters"),
        (42784u32, 43008u32, "Latin Extended-D"),
        (43008u32, 43056u32, "Syloti Nagri"),
        (43056u32, 43072u32, "Common Indic Number Forms"),
        (43072u32, 43136u32, "Phags-pa"),
        (43136u32, 43232u32, "Saurashtra"),
        (43232u32, 43264u32, "Devanagari Extended"),
        (43264u32, 43312u32, "Kayah Li"),
        (43312u32, 43360u32, "Rejang"),
        (43360u32, 43392u32, "Hangul Jamo Extended-A"),
        (43392u32, 43488u32, "Javanese"),
        (43488u32, 43520u32, "Myanmar Extended-B"),
        (43520u32, 43616u32, "Cham"),
        (43616u32, 43648u32, "Myanmar Extended-A"),
        (43648u32, 43744u32, "Tai Viet"),
        (43744u32, 43776u32, "Meetei Mayek Extensions"),
        (43776u32, 43824u32, "Ethiopic Extended-A"),
        (43824u32, 43888u32, "Latin Extended-E"),
        (43888u32, 43968u32, "Cherokee Supplement"),
        (43968u32, 44032u32, "Meetei Mayek"),
        (44032u32, 55216u32, "Hangul Syllables"),
        (55216u32, 55296u32, "Hangul Jamo Extended-B"),
        (55296u32, 56192u32, "High Surrogates"),
        (56192u32, 56320u32, "High Private Use Surrogates"),
        (56320u32, 57344u32, "Low Surrogates"),
        (57344u32, 63744u32, "Private Use Area"),
        (63744u32, 64256u32, "CJK Compatibility Ideographs"),
        (64256u32, 64336u32, "Alphabetic Presentation Forms"),
        (64336u32, 65024u32, "Arabic Presentation Forms-A"),
        (65024u32, 65040u32, "Variation Selectors"),
        (65040u32, 65056u32, "Vertical Forms"),
        (65056u32, 65072u32, "Combining Half Marks"),
        (65072u32, 65104u32, "CJK Compatibility Forms"),
        (65104u32, 65136u32, "Small Form Variants"),
        (65136u32, 65280u32, "Arabic Presentation Forms-B"),
        (65280u32, 65520u32, "Halfwidth and Fullwidth Forms"),
        (65520u32, 65536u32, "Specials"),
        (65536u32, 65664u32, "Linear B Syllabary"),
        (65664u32, 65792u32, "Linear B Ideograms"),
        (65792u32, 65856u32, "Aegean Numbers"),
        (65856u32, 65936u32, "Ancient Greek Numbers"),
        (65936u32, 66000u32, "Ancient Symbols"),
        (66000u32, 66048u32, "Phaistos Disc"),
        (66176u32, 66208u32, "Lycian"),
        (66208u32, 66272u32, "Carian"),
        (66272u32, 66304u32, "Coptic Epact Numbers"),
        (66304u32, 66352u32, "Old Italic"),
        (66352u32, 66384u32, "Gothic"),
        (66384u32, 66432u32, "Old Permic"),
        (66432u32, 66464u32, "Ugaritic"),
        (66464u32, 66528u32, "Old Persian"),
        (66560u32, 66640u32, "Deseret"),
        (66640u32, 66688u32, "Shavian"),
        (66688u32, 66736u32, "Osmanya"),
        (66736u32, 66816u32, "Osage"),
        (66816u32, 66864u32, "Elbasan"),
        (66864u32, 66928u32, "Caucasian Albanian"),
        (66928u32, 67008u32, "Vithkuqi"),
        (67008u32, 67072u32, "Todhri"),
        (67072u32, 67456u32, "Linear A"),
        (67456u32, 67520u32, "Latin Extended-F"),
        (67584u32, 67648u32, "Cypriot Syllabary"),
        (67648u32, 67680u32, "Imperial Aramaic"),
        (67680u32, 67712u32, "Palmyrene"),
        (67712u32, 67760u32, "Nabataean"),
        (67808u32, 67840u32, "Hatran"),
        (67840u32, 67872u32, "Phoenician"),
        (67872u32, 67904u32, "Lydian"),
        (67904u32, 67936u32, "Sidetic"),
        (67968u32, 68000u32, "Meroitic Hieroglyphs"),
        (68000u32, 68096u32, "Meroitic Cursive"),
        (68096u32, 68192u32, "Kharoshthi"),
        (68192u32, 68224u32, "Old South Arabian"),
        (68224u32, 68256u32, "Old North Arabian"),
        (68288u32, 68352u32, "Manichaean"),
        (68352u32, 68416u32, "Avestan"),
        (68416u32, 68448u32, "Inscriptional Parthian"),
        (68448u32, 68480u32, "Inscriptional Pahlavi"),
        (68480u32, 68528u32, "Psalter Pahlavi"),
        (68608u32, 68688u32, "Old Turkic"),
        (68736u32, 68864u32, "Old Hungarian"),
        (68864u32, 68928u32, "Hanifi Rohingya"),
        (68928u32, 69008u32, "Garay"),
        (69216u32, 69248u32, "Rumi Numeral Symbols"),
        (69248u32, 69312u32, "Yezidi"),
        (69312u32, 69376u32, "Arabic Extended-C"),
        (69376u32, 69424u32, "Old Sogdian"),
        (69424u32, 69488u32, "Sogdian"),
        (69488u32, 69552u32, "Old Uyghur"),
        (69552u32, 69600u32, "Chorasmian"),
        (69600u32, 69632u32, "Elymaic"),
        (69632u32, 69760u32, "Brahmi"),
        (69760u32, 69840u32, "Kaithi"),
        (69840u32, 69888u32, "Sora Sompeng"),
        (69888u32, 69968u32, "Chakma"),
        (69968u32, 70016u32, "Mahajani"),
        (70016u32, 70112u32, "Sharada"),
        (70112u32, 70144u32, "Sinhala Archaic Numbers"),
        (70144u32, 70224u32, "Khojki"),
        (70272u32, 70320u32, "Multani"),
        (70320u32, 70400u32, "Khudawadi"),
        (70400u32, 70528u32, "Grantha"),
        (70528u32, 70656u32, "Tulu-Tigalari"),
        (70656u32, 70784u32, "Newa"),
        (70784u32, 70880u32, "Tirhuta"),
        (71040u32, 71168u32, "Siddham"),
        (71168u32, 71264u32, "Modi"),
        (71264u32, 71296u32, "Mongolian Supplement"),
        (71296u32, 71376u32, "Takri"),
        (71376u32, 71424u32, "Myanmar Extended-C"),
        (71424u32, 71504u32, "Ahom"),
        (71680u32, 71760u32, "Dogra"),
        (71840u32, 71936u32, "Warang Citi"),
        (71936u32, 72032u32, "Dives Akuru"),
        (72096u32, 72192u32, "Nandinagari"),
        (72192u32, 72272u32, "Zanabazar Square"),
        (72272u32, 72368u32, "Soyombo"),
        (
            72368u32,
            72384u32,
            "Unified Canadian Aboriginal Syllabics Extended-A",
        ),
        (72384u32, 72448u32, "Pau Cin Hau"),
        (72448u32, 72544u32, "Devanagari Extended-A"),
        (72544u32, 72576u32, "Sharada Supplement"),
        (72640u32, 72704u32, "Sunuwar"),
        (72704u32, 72816u32, "Bhaiksuki"),
        (72816u32, 72896u32, "Marchen"),
        (72960u32, 73056u32, "Masaram Gondi"),
        (73056u32, 73136u32, "Gunjala Gondi"),
        (73136u32, 73200u32, "Tolong Siki"),
        (73440u32, 73472u32, "Makasar"),
        (73472u32, 73568u32, "Kawi"),
        (73648u32, 73664u32, "Lisu Supplement"),
        (73664u32, 73728u32, "Tamil Supplement"),
        (73728u32, 74752u32, "Cuneiform"),
        (74752u32, 74880u32, "Cuneiform Numbers and Punctuation"),
        (74880u32, 75088u32, "Early Dynastic Cuneiform"),
        (77712u32, 77824u32, "Cypro-Minoan"),
        (77824u32, 78896u32, "Egyptian Hieroglyphs"),
        (78896u32, 78944u32, "Egyptian Hieroglyph Format Controls"),
        (78944u32, 82944u32, "Egyptian Hieroglyphs Extended-A"),
        (82944u32, 83584u32, "Anatolian Hieroglyphs"),
        (90368u32, 90432u32, "Gurung Khema"),
        (92160u32, 92736u32, "Bamum Supplement"),
        (92736u32, 92784u32, "Mro"),
        (92784u32, 92880u32, "Tangsa"),
        (92880u32, 92928u32, "Bassa Vah"),
        (92928u32, 93072u32, "Pahawh Hmong"),
        (93504u32, 93568u32, "Kirat Rai"),
        (93760u32, 93856u32, "Medefaidrin"),
        (93856u32, 93920u32, "Beria Erfe"),
        (93952u32, 94112u32, "Miao"),
        (94176u32, 94208u32, "Ideographic Symbols and Punctuation"),
        (94208u32, 100352u32, "Tangut"),
        (100352u32, 101120u32, "Tangut Components"),
        (101120u32, 101632u32, "Khitan Small Script"),
        (101632u32, 101760u32, "Tangut Supplement"),
        (101760u32, 101888u32, "Tangut Components Supplement"),
        (110576u32, 110592u32, "Kana Extended-B"),
        (110592u32, 110848u32, "Kana Supplement"),
        (110848u32, 110896u32, "Kana Extended-A"),
        (110896u32, 110960u32, "Small Kana Extension"),
        (110960u32, 111360u32, "Nushu"),
        (113664u32, 113824u32, "Duployan"),
        (113824u32, 113840u32, "Shorthand Format Controls"),
        (
            117760u32,
            118464u32,
            "Symbols for Legacy Computing Supplement",
        ),
        (118464u32, 118528u32, "Miscellaneous Symbols Supplement"),
        (118528u32, 118736u32, "Znamenny Musical Notation"),
        (118784u32, 119040u32, "Byzantine Musical Symbols"),
        (119040u32, 119296u32, "Musical Symbols"),
        (119296u32, 119376u32, "Ancient Greek Musical Notation"),
        (119488u32, 119520u32, "Kaktovik Numerals"),
        (119520u32, 119552u32, "Mayan Numerals"),
        (119552u32, 119648u32, "Tai Xuan Jing Symbols"),
        (119648u32, 119680u32, "Counting Rod Numerals"),
        (119808u32, 120832u32, "Mathematical Alphanumeric Symbols"),
        (120832u32, 121520u32, "Sutton SignWriting"),
        (122624u32, 122880u32, "Latin Extended-G"),
        (122880u32, 122928u32, "Glagolitic Supplement"),
        (122928u32, 123024u32, "Cyrillic Extended-D"),
        (123136u32, 123216u32, "Nyiakeng Puachue Hmong"),
        (123536u32, 123584u32, "Toto"),
        (123584u32, 123648u32, "Wancho"),
        (124112u32, 124160u32, "Nag Mundari"),
        (124368u32, 124416u32, "Ol Onal"),
        (124608u32, 124672u32, "Tai Yo"),
        (124896u32, 124928u32, "Ethiopic Extended-B"),
        (124928u32, 125152u32, "Mende Kikakui"),
        (125184u32, 125280u32, "Adlam"),
        (126064u32, 126144u32, "Indic Siyaq Numbers"),
        (126208u32, 126288u32, "Ottoman Siyaq Numbers"),
        (
            126464u32,
            126720u32,
            "Arabic Mathematical Alphabetic Symbols",
        ),
        (126976u32, 127024u32, "Mahjong Tiles"),
        (127024u32, 127136u32, "Domino Tiles"),
        (127136u32, 127232u32, "Playing Cards"),
        (127232u32, 127488u32, "Enclosed Alphanumeric Supplement"),
        (127488u32, 127744u32, "Enclosed Ideographic Supplement"),
        (
            127744u32,
            128512u32,
            "Miscellaneous Symbols and Pictographs",
        ),
        (128512u32, 128592u32, "Emoticons"),
        (128592u32, 128640u32, "Ornamental Dingbats"),
        (128640u32, 128768u32, "Transport and Map Symbols"),
        (128768u32, 128896u32, "Alchemical Symbols"),
        (128896u32, 129024u32, "Geometric Shapes Extended"),
        (129024u32, 129280u32, "Supplemental Arrows-C"),
        (129280u32, 129536u32, "Supplemental Symbols and Pictographs"),
        (129536u32, 129648u32, "Chess Symbols"),
        (129648u32, 129792u32, "Symbols and Pictographs Extended-A"),
        (129792u32, 130048u32, "Symbols for Legacy Computing"),
        (131072u32, 173792u32, "CJK Unified Ideographs Extension B"),
        (173824u32, 177984u32, "CJK Unified Ideographs Extension C"),
        (177984u32, 178208u32, "CJK Unified Ideographs Extension D"),
        (178208u32, 183984u32, "CJK Unified Ideographs Extension E"),
        (183984u32, 191472u32, "CJK Unified Ideographs Extension F"),
        (191472u32, 192096u32, "CJK Unified Ideographs Extension I"),
        (
            194560u32,
            195104u32,
            "CJK Compatibility Ideographs Supplement",
        ),
        (196608u32, 201552u32, "CJK Unified Ideographs Extension G"),
        (201552u32, 205744u32, "CJK Unified Ideographs Extension H"),
        (205744u32, 210048u32, "CJK Unified Ideographs Extension J"),
        (917504u32, 917632u32, "Tags"),
        (917760u32, 918000u32, "Variation Selectors Supplement"),
        (983040u32, 1048576u32, "Supplementary Private Use Area-A"),
        (1048576u32, 1114112u32, "Supplementary Private Use Area-B"),
    ]
});

pub static FREQUENCIES: Lazy<HashMap<&'static str, Vec<&'static str>>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert(
        "English",
        vec![
            "e", "a", "t", "i", "o", "n", "s", "r", "h", "l", "d", "c", "u", "m", "f", "p", "g",
            "w", "y", "b", "v", "k", "x", "j", "z", "q",
        ],
    );
    m.insert(
        "English—",
        vec![
            "e", "a", "t", "i", "o", "n", "s", "r", "h", "l", "d", "c", "m", "u", "f", "p", "g",
            "w", "b", "y", "v", "k", "j", "x", "z", "q",
        ],
    );
    m.insert(
        "German",
        vec![
            "e", "n", "i", "r", "s", "t", "a", "d", "h", "u", "l", "g", "o", "c", "m", "b", "f",
            "k", "w", "z", "p", "v", "ü", "ä", "ö", "j",
        ],
    );
    m.insert(
        "French",
        vec![
            "e", "a", "s", "n", "i", "t", "r", "l", "u", "o", "d", "c", "p", "m", "é", "v", "g",
            "f", "b", "h", "q", "à", "x", "è", "y", "j",
        ],
    );
    m.insert(
        "Dutch",
        vec![
            "e", "n", "a", "i", "r", "t", "o", "d", "s", "l", "g", "h", "v", "m", "u", "k", "c",
            "p", "b", "w", "j", "z", "f", "y", "x", "ë",
        ],
    );
    m.insert(
        "Italian",
        vec![
            "e", "i", "a", "o", "n", "l", "t", "r", "s", "c", "d", "u", "p", "m", "g", "v", "f",
            "b", "z", "h", "q", "è", "à", "k", "y", "ò",
        ],
    );
    m.insert(
        "Polish",
        vec![
            "a", "i", "o", "e", "n", "r", "z", "w", "s", "c", "t", "k", "y", "d", "p", "m", "u",
            "l", "j", "ł", "g", "b", "h", "ą", "ę", "ó",
        ],
    );
    m.insert(
        "Spanish",
        vec![
            "e", "a", "o", "n", "s", "r", "i", "l", "d", "t", "c", "u", "m", "p", "b", "g", "v",
            "f", "y", "ó", "h", "q", "í", "j", "z", "á",
        ],
    );
    m.insert(
        "Russian",
        vec![
            "о", "е", "а", "и", "н", "т", "с", "р", "в", "л", "к", "м", "д", "п", "у", "г", "я",
            "ы", "з", "б", "й", "ь", "ч", "х", "ж", "ц",
        ],
    );
    m.insert(
        "Japanese",
        vec![
            "日", "一", "人", "年", "大", "十", "二", "本", "中", "長", "出", "三", "時", "行",
            "見", "月", "分", "後", "前", "生", "五", "間", "上", "東", "四", "今", "金", "九",
            "入", "学", "高", "円", "子", "外", "八", "六", "下", "来", "気", "小", "七", "山",
            "話", "女", "北", "午", "百", "書", "先", "名", "川", "千", "水", "半", "男", "西",
            "電", "校", "語", "土", "木", "聞", "食", "車", "何", "南", "万", "毎", "白", "天",
            "母", "火", "右", "読", "友", "左", "休", "父", "雨",
        ],
    );
    m.insert(
        "Japanese—",
        vec![
            "ー", "ン", "ス", "・", "ル", "ト", "リ", "イ", "ア", "ラ", "ッ", "ク", "ド", "シ",
            "レ", "ジ", "タ", "フ", "ロ", "カ", "テ", "マ", "ィ", "グ", "バ", "ム", "プ", "オ",
            "コ", "デ", "ニ", "ウ", "メ", "サ", "ビ", "ナ", "ブ", "ャ", "エ", "ュ", "チ", "キ",
            "ズ", "ダ", "パ", "ミ", "ェ", "ョ", "ハ", "セ", "ベ", "ガ", "モ", "ツ", "ネ", "ボ",
            "ソ", "ノ", "ァ", "ヴ", "ワ", "ポ", "ペ", "ピ", "ケ", "ゴ", "ギ", "ザ", "ホ", "ゲ",
            "ォ", "ヤ", "ヒ", "ユ", "ヨ", "ヘ", "ゼ", "ヌ", "ゥ", "ゾ", "ヶ", "ヂ", "ヲ", "ヅ",
            "ヵ", "ヱ", "ヰ", "ヮ", "ヽ", "゠", "ヾ", "ヷ", "ヿ", "ヸ", "ヹ", "ヺ",
        ],
    );
    m.insert(
        "Japanese——",
        vec![
            "の", "に", "る", "た", "と", "は", "し", "い", "を", "で", "て", "が", "な", "れ",
            "か", "ら", "さ", "っ", "り", "す", "あ", "も", "こ", "ま", "う", "く", "よ", "き",
            "ん", "め", "お", "け", "そ", "つ", "だ", "や", "え", "ど", "わ", "ち", "み", "せ",
            "じ", "ば", "へ", "び", "ず", "ろ", "ほ", "げ", "む", "べ", "ひ", "ょ", "ゆ", "ぶ",
            "ご", "ゃ", "ね", "ふ", "ぐ", "ぎ", "ぼ", "ゅ", "づ", "ざ", "ぞ", "ぬ", "ぜ", "ぱ",
            "ぽ", "ぷ", "ぴ", "ぃ", "ぁ", "ぇ", "ぺ", "ゞ", "ぢ", "ぉ", "ぅ", "ゐ", "ゝ", "ゑ",
            "゛", "゜", "ゎ", "ゔ", "゚", "ゟ", "゙", "ゕ", "ゖ",
        ],
    );
    m.insert(
        "Portuguese",
        vec![
            "a", "e", "o", "s", "i", "r", "d", "n", "t", "m", "u", "c", "l", "p", "g", "v", "b",
            "f", "h", "ã", "q", "é", "ç", "á", "z", "í",
        ],
    );
    m.insert(
        "Swedish",
        vec![
            "e", "a", "n", "r", "t", "s", "i", "l", "d", "o", "m", "k", "g", "v", "h", "f", "u",
            "p", "ä", "c", "b", "ö", "å", "y", "j", "x",
        ],
    );
    m.insert(
        "Chinese",
        vec![
            "的", "一", "是", "不", "了", "在", "人", "有", "我", "他", "这", "个", "们", "中",
            "来", "上", "大", "为", "和", "国", "地", "到", "以", "说", "时", "要", "就", "出",
            "会", "可", "也", "你", "对", "生", "能", "而", "子", "那", "得", "于", "着", "下",
            "自", "之", "年", "过", "发", "后", "作", "里", "用", "道", "行", "所", "然", "家",
            "种", "事", "成", "方", "多", "经", "么", "去", "法", "学", "如", "都", "同", "现",
            "当", "没", "动", "面", "起", "看", "定", "天", "分", "还", "进", "好", "小", "部",
            "其", "些", "主", "样", "理", "心", "她", "本", "前", "开", "但", "因", "只", "从",
            "想", "实",
        ],
    );
    m.insert(
        "Ukrainian",
        vec![
            "о", "а", "н", "і", "и", "р", "в", "т", "е", "с", "к", "л", "у", "д", "м", "п", "з",
            "я", "ь", "б", "г", "й", "ч", "х", "ц", "ї",
        ],
    );
    m.insert(
        "Norwegian",
        vec![
            "e", "r", "n", "t", "a", "s", "i", "o", "l", "d", "g", "k", "m", "v", "f", "p", "u",
            "b", "h", "å", "y", "j", "ø", "c", "æ", "w",
        ],
    );
    m.insert(
        "Finnish",
        vec![
            "a", "i", "n", "t", "e", "s", "l", "o", "u", "k", "ä", "m", "r", "v", "j", "h", "p",
            "y", "d", "ö", "g", "c", "b", "f", "w", "z",
        ],
    );
    m.insert(
        "Vietnamese",
        vec![
            "n", "h", "t", "i", "c", "g", "a", "o", "u", "m", "l", "r", "à", "đ", "s", "e", "v",
            "p", "b", "y", "ư", "d", "á", "k", "ộ", "ế",
        ],
    );
    m.insert(
        "Czech",
        vec![
            "o", "e", "a", "n", "t", "s", "i", "l", "v", "r", "k", "d", "u", "m", "p", "í", "c",
            "h", "z", "á", "y", "j", "b", "ě", "é", "ř",
        ],
    );
    m.insert(
        "Hungarian",
        vec![
            "e", "a", "t", "l", "s", "n", "k", "r", "i", "o", "z", "á", "é", "g", "m", "b", "y",
            "v", "d", "h", "u", "p", "j", "ö", "f", "c",
        ],
    );
    m.insert(
        "Korean",
        vec![
            "이", "다", "에", "의", "는", "로", "하", "을", "가", "고", "지", "서", "한", "은",
            "기", "으", "년", "대", "사", "시", "를", "리", "도", "인", "스", "일",
        ],
    );
    m.insert(
        "Indonesian",
        vec![
            "a", "n", "e", "i", "r", "t", "u", "s", "d", "k", "m", "l", "g", "p", "b", "o", "h",
            "y", "j", "c", "w", "f", "v", "z", "x", "q",
        ],
    );
    m.insert(
        "Turkish",
        vec![
            "a", "e", "i", "n", "r", "l", "ı", "k", "d", "t", "s", "m", "y", "u", "o", "b", "ü",
            "ş", "v", "g", "z", "h", "c", "p", "ç", "ğ",
        ],
    );
    m.insert(
        "Romanian",
        vec![
            "e", "i", "a", "r", "n", "t", "u", "l", "o", "c", "s", "d", "p", "m", "ă", "f", "v",
            "î", "g", "b", "ș", "ț", "z", "h", "â", "j",
        ],
    );
    m.insert(
        "Farsi",
        vec![
            "ا", "ی", "ر", "د", "ن", "ه", "و", "م", "ت", "ب", "س", "ل", "ک", "ش", "ز", "ف", "گ",
            "ع", "خ", "ق", "ج", "آ", "پ", "ح", "ط", "ص",
        ],
    );
    m.insert(
        "Arabic",
        vec![
            "ا", "ل", "ي", "م", "و", "ن", "ر", "ت", "ب", "ة", "ع", "د", "س", "ف", "ه", "ك", "ق",
            "أ", "ح", "ج", "ش", "ط", "ص", "ى", "خ", "إ",
        ],
    );
    m.insert(
        "Danish",
        vec![
            "e", "r", "n", "t", "a", "i", "s", "d", "l", "o", "g", "m", "k", "f", "v", "u", "b",
            "h", "p", "å", "y", "ø", "æ", "c", "j", "w",
        ],
    );
    m.insert(
        "Serbian",
        vec![
            "а", "и", "о", "е", "н", "р", "с", "у", "т", "к", "ј", "в", "д", "м", "п", "л", "г",
            "з", "б", "a", "i", "e", "o", "n", "ц", "ш",
        ],
    );
    m.insert(
        "Lithuanian",
        vec![
            "i", "a", "s", "o", "r", "e", "t", "n", "u", "k", "m", "l", "p", "v", "d", "j", "g",
            "ė", "b", "y", "ų", "š", "ž", "c", "ą", "į",
        ],
    );
    m.insert(
        "Slovene",
        vec![
            "e", "a", "i", "o", "n", "r", "s", "l", "t", "j", "v", "k", "d", "p", "m", "u", "z",
            "b", "g", "h", "č", "c", "š", "ž", "f", "y",
        ],
    );
    m.insert(
        "Slovak",
        vec![
            "o", "a", "e", "n", "i", "r", "v", "t", "s", "l", "k", "d", "m", "p", "u", "c", "h",
            "j", "b", "z", "á", "y", "ý", "í", "č", "é",
        ],
    );
    m.insert(
        "Hebrew",
        vec![
            "י", "ו", "ה", "ל", "ר", "ב", "ת", "מ", "א", "ש", "נ", "ע", "ם", "ד", "ק", "ח", "פ",
            "ס", "כ", "ג", "ט", "צ", "ן", "ז", "ך",
        ],
    );
    m.insert(
        "Bulgarian",
        vec![
            "а", "и", "о", "е", "н", "т", "р", "с", "в", "л", "к", "д", "п", "м", "з", "г", "я",
            "ъ", "у", "б", "ч", "ц", "й", "ж", "щ", "х",
        ],
    );
    m.insert(
        "Croatian",
        vec![
            "a", "i", "o", "e", "n", "r", "j", "s", "t", "u", "k", "l", "v", "d", "m", "p", "g",
            "z", "b", "c", "č", "h", "š", "ž", "ć", "f",
        ],
    );
    m.insert(
        "Hindi",
        vec![
            "क", "र", "स", "न", "त", "म", "ह", "प", "य", "ल", "व", "ज", "द", "ग", "ब", "श", "ट",
            "अ", "ए", "थ", "भ", "ड", "च", "ध", "ष", "इ",
        ],
    );
    m.insert(
        "Estonian",
        vec![
            "a", "i", "e", "s", "t", "l", "u", "n", "o", "k", "r", "d", "m", "v", "g", "p", "j",
            "h", "ä", "b", "õ", "ü", "f", "c", "ö", "y",
        ],
    );
    m.insert(
        "Thai",
        vec![
            "า", "น", "ร", "อ", "ก", "เ", "ง", "ม", "ย", "ล", "ว", "ด", "ท", "ส", "ต", "ะ", "ป",
            "บ", "ค", "ห", "แ", "จ", "พ", "ช", "ข", "ใ",
        ],
    );
    m.insert(
        "Greek",
        vec![
            "α", "τ", "ο", "ι", "ε", "ν", "ρ", "σ", "κ", "η", "π", "ς", "υ", "μ", "λ", "ί", "ό",
            "ά", "γ", "έ", "δ", "ή", "ω", "χ", "θ", "ύ",
        ],
    );
    m.insert(
        "Tamil",
        vec![
            "க", "த", "ப", "ட", "ர", "ம", "ல", "ன", "வ", "ற", "ய", "ள", "ச", "ந", "இ", "ண", "அ",
            "ஆ", "ழ", "ங", "எ", "உ", "ஒ", "ஸ",
        ],
    );
    m.insert(
        "Kazakh",
        vec![
            "а", "ы", "е", "н", "т", "р", "л", "і", "д", "с", "м", "қ", "к", "о", "б", "и", "у",
            "ғ", "ж", "ң", "з", "ш", "й", "п", "г", "ө",
        ],
    );
    m
});

pub static _FREQUENCIES_RANK: Lazy<HashMap<&'static str, HashMap<&'static str, usize>>> =
    Lazy::new(|| {
        FREQUENCIES
            .iter()
            .map(|(lang, chars)| {
                let r: HashMap<&str, usize> =
                    chars.iter().enumerate().map(|(i, &c)| (c, i)).collect();
                (*lang, r)
            })
            .collect()
    });

pub static _FREQUENCIES_SET: Lazy<HashMap<&'static str, HashSet<&'static str>>> = Lazy::new(|| {
    FREQUENCIES
        .iter()
        .map(|(lang, chars)| (*lang, chars.iter().copied().collect()))
        .collect()
});

pub static IANA_SUPPORTED: Lazy<Vec<&'static str>> = Lazy::new(|| {
    vec![
        "ascii",
        "big5",
        "big5hkscs",
        "cp037",
        "cp1006",
        "cp1026",
        "cp1125",
        "cp1140",
        "cp1250",
        "cp1251",
        "cp1252",
        "cp1253",
        "cp1254",
        "cp1255",
        "cp1256",
        "cp1257",
        "cp1258",
        "cp273",
        "cp424",
        "cp437",
        "cp500",
        "cp720",
        "cp737",
        "cp775",
        "cp850",
        "cp852",
        "cp855",
        "cp856",
        "cp857",
        "cp858",
        "cp860",
        "cp861",
        "cp862",
        "cp863",
        "cp864",
        "cp865",
        "cp866",
        "cp869",
        "cp874",
        "cp874",
        "cp875",
        "cp932",
        "cp949",
        "cp950",
        "euc_jis_2004",
        "euc_jisx0213",
        "euc_jp",
        "euc_kr",
        "gb18030",
        "gb2312",
        "gbk",
        "hp_roman8",
        "hz",
        "iso2022_jp",
        "iso2022_jp_1",
        "iso2022_jp_2",
        "iso2022_jp_2004",
        "iso2022_jp_3",
        "iso2022_jp_ext",
        "iso2022_kr",
        "iso8859_10",
        "iso8859_11",
        "iso8859_13",
        "iso8859_14",
        "iso8859_15",
        "iso8859_16",
        "iso8859_2",
        "iso8859_3",
        "iso8859_4",
        "iso8859_5",
        "iso8859_6",
        "iso8859_7",
        "iso8859_8",
        "iso8859_9",
        "johab",
        "koi8_r",
        "koi8_r",
        "koi8_t",
        "koi8_u",
        "kz1048",
        "latin_1",
        "mac_cyrillic",
        "mac_greek",
        "mac_iceland",
        "mac_latin2",
        "mac_roman",
        "mac_turkish",
        "ptcp154",
        "shift_jis",
        "shift_jis_2004",
        "shift_jisx0213",
        "tis_620",
        "utf_16",
        "utf_16_be",
        "utf_16_le",
        "utf_32",
        "utf_32_be",
        "utf_32_le",
        "utf_7",
        "utf_8",
    ]
});

pub const IANA_SUPPORTED_COUNT: usize = 100;

pub const IANA_SUPPORTED_SIMILAR: phf::Map<&'static str, &'static [&'static str]> = phf::phf_map! {
    "cp037" => &["cp1026", "cp1140", "cp273", "cp500"],
    "cp1026" => &["cp037", "cp1140", "cp273", "cp500"],
    "cp1125" => &["cp866"],
    "cp1140" => &["cp037", "cp1026", "cp273", "cp500"],
    "cp1250" => &["iso8859_2"],
    "cp1251" => &["kz1048", "ptcp154"],
    "cp1252" => &["iso8859_15", "iso8859_9", "latin_1"],
    "cp1253" => &["iso8859_7"],
    "cp1254" => &["iso8859_15", "iso8859_9", "latin_1"],
    "cp1257" => &["iso8859_13"],
    "cp273" => &["cp037", "cp1026", "cp1140", "cp500"],
    "cp437" => &["cp850", "cp858", "cp860", "cp861", "cp862", "cp863", "cp865"],
    "cp500" => &["cp037", "cp1026", "cp1140", "cp273"],
    "cp850" => &["cp437", "cp857", "cp858", "cp865"],
    "cp857" => &["cp850", "cp858", "cp865"],
    "cp858" => &["cp437", "cp850", "cp857", "cp865"],
    "cp860" => &["cp437", "cp861", "cp862", "cp863", "cp865"],
    "cp861" => &["cp437", "cp860", "cp862", "cp863", "cp865"],
    "cp862" => &["cp437", "cp860", "cp861", "cp863", "cp865"],
    "cp863" => &["cp437", "cp860", "cp861", "cp862", "cp863", "cp865"],
    "cp865" => &["cp437", "cp850", "cp857", "cp858", "cp860", "cp861", "cp862", "cp863"],
    "cp866" => &["cp1125"],
    "iso8859_10" => &["iso8859_14", "iso8859_15", "iso8859_4", "iso8859_9", "latin_1"],
    "iso8859_11" => &["tis_620"],
    "iso8859_13" => &["cp1257"],
    "iso8859_14" => &["iso8859_10", "iso8859_15", "iso8859_16", "iso8859_3", "iso8859_9", "latin_1"],
    "iso8859_15" => &["cp1252", "cp1254", "iso8859_10", "iso8859_14", "iso8859_16", "iso8859_3", "iso8859_9", "latin_1"],
    "iso8859_16" => &["iso8859_14", "iso8859_15", "iso8859_2", "iso8859_3", "iso8859_9", "latin_1"],
    "iso8859_2" => &["cp1250", "iso8859_16", "iso8859_4"],
    "iso8859_3" => &["iso8859_14", "iso8859_15", "iso8859_16", "iso8859_9", "latin_1"],
    "iso8859_4" => &["iso8859_10", "iso8859_2", "iso8859_9", "latin_1"],
    "iso8859_7" => &["cp1253"],
    "iso8859_9" => &["cp1252", "cp1254", "cp1258", "iso8859_10", "iso8859_14", "iso8859_15", "iso8859_16", "iso8859_3", "iso8859_4", "latin_1"],
    "kz1048" => &["cp1251", "ptcp154"],
    "latin_1" => &["cp1252", "cp1254", "cp1258", "iso8859_10", "iso8859_14", "iso8859_15", "iso8859_16", "iso8859_3", "iso8859_4", "iso8859_9"],
    "mac_iceland" => &["mac_roman", "mac_turkish"],
    "mac_roman" => &["mac_iceland", "mac_turkish"],
    "mac_turkish" => &["mac_iceland", "mac_roman"],
    "ptcp154" => &["cp1251", "kz1048"],
    "tis_620" => &["iso8859_11"],
};

pub const LANGUAGE_SUPPORTED_COUNT: usize = 41;

pub const _LATIN: u32 = 1;
pub const _ACCENTUATED: u32 = 1 << 1;
pub const _CJK: u32 = 1 << 2;
pub const _HANGUL: u32 = 1 << 3;
pub const _KATAKANA: u32 = 1 << 4;
pub const _HIRAGANA: u32 = 1 << 5;
pub const _THAI: u32 = 1 << 6;
pub const _ARABIC: u32 = 1 << 7;
pub const _ARABIC_ISOLATED_FORM: u32 = 1 << 8;

pub static _ACCENT_KEYWORDS: Lazy<Vec<&'static str>> = Lazy::new(|| {
    vec![
        "WITH GRAVE",
        "WITH ACUTE",
        "WITH CEDILLA",
        "WITH DIAERESIS",
        "WITH CIRCUMFLEX",
        "WITH TILDE",
        "WITH MACRON",
        "WITH RING ABOVE",
    ]
});

pub static UNICODE_SECONDARY_RANGE_KEYWORD: Lazy<Vec<&'static str>> = Lazy::new(|| {
    vec![
        "Supplement",
        "Extended",
        "Extensions",
        "Modifier",
        "Marks",
        "Punctuation",
        "Symbols",
        "Forms",
        "Operators",
        "Miscellaneous",
        "Drawing",
        "Block",
        "Shapes",
        "Supplemental",
        "Tags",
    ]
});

pub static COMMON_SAFE_ASCII_CHARACTERS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "<", ">", "=", ":", "/", "&", ";", "{", "}", "[", "]", ",", "|", "\"", "-", "(", ")",
    ]
    .iter()
    .copied()
    .collect()
});

pub static COMMON_CJK_CHARACTERS: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    [
        "一", "七", "万", "三", "上", "下", "不", "与", "专", "且", "世", "业", "东", "两", "严",
        "个", "中", "为", "主", "么", "义", "之", "九", "也", "习", "书", "了", "争", "事", "二",
        "于", "五", "些", "交", "产", "京", "亲", "人", "什", "今", "从", "他", "代", "以", "们",
        "件", "价", "任", "休", "众", "会", "传", "但", "位", "低", "住", "体", "何", "作", "你",
        "使", "例", "便", "保", "信", "候", "值", "做", "儿", "元", "先", "光", "克", "党", "入",
        "全", "八", "公", "六", "共", "关", "其", "具", "养", "内", "円", "再", "写", "军", "农",
        "决", "况", "准", "几", "出", "分", "切", "划", "列", "则", "利", "别", "到", "制", "前",
        "力", "办", "加", "务", "动", "劳", "包", "化", "北", "区", "十", "千", "午", "半", "华",
        "单", "南", "即", "却", "厂", "历", "压", "原", "去", "县", "参", "又", "及", "友", "反",
        "发", "取", "受", "变", "口", "只", "叫", "可", "史", "右", "号", "各", "合", "同", "名",
        "后", "向", "听", "员", "周", "命", "和", "品", "响", "商", "器", "四", "回", "因", "团",
        "国", "图", "圆", "國", "土", "在", "地", "场", "型", "基", "增", "声", "处", "备", "复",
        "外", "多", "大", "天", "太", "头", "女", "她", "好", "如", "始", "委", "子", "存", "学",
        "學", "它", "安", "完", "定", "实", "家", "容", "对", "导", "将", "小", "少", "就", "局",
        "层", "展", "属", "山", "川", "工", "左", "己", "已", "市", "布", "带", "常", "干", "平",
        "年", "并", "广", "应", "府", "度", "建", "开", "式", "引", "张", "强", "当", "形", "影",
        "往", "很", "律", "後", "得", "心", "必", "志", "快", "思", "性", "总", "情", "想", "意",
        "感", "成", "我", "或", "战", "所", "手", "才", "打", "技", "把", "报", "拉", "持", "指",
        "按", "据", "接", "提", "支", "收", "改", "放", "政", "效", "教", "数", "整", "文", "斗",
        "料", "断", "斯", "新", "方", "族", "无", "日", "时", "明", "易", "是", "時", "更", "書",
        "最", "月", "有", "期", "木", "本", "术", "机", "权", "条", "来", "東", "极", "构", "林",
        "果", "查", "标", "校", "样", "根", "格", "次", "正", "此", "步", "段", "母", "毎", "每",
        "比", "毛", "民", "气", "気", "水", "求", "江", "没", "油", "治", "法", "活", "派", "流",
        "济", "海", "消", "深", "清", "温", "满", "火", "点", "热", "然", "照", "父", "片", "物",
        "特", "状", "率", "王", "现", "理", "生", "用", "由", "电", "男", "界", "白", "百", "的",
        "目", "直", "相", "省", "看", "真", "眼", "着", "知", "石", "矿", "研", "确", "示", "社",
        "离", "种", "科", "积", "程", "究", "空", "立", "第", "等", "算", "管", "米", "类", "精",
        "系", "素", "红", "约", "级", "线", "组", "细", "织", "经", "结", "给", "统", "维", "置",
        "美", "群", "老", "者", "而", "联", "聞", "育", "能", "自", "至", "般", "色", "节", "花",
        "萬", "行", "表", "被", "装", "西", "要", "見", "见", "观", "规", "角", "解", "話", "語",
        "読", "计", "认", "议", "记", "许", "论", "设", "证", "识", "话", "该", "说", "调", "象",
        "质", "资", "走", "起", "越", "路", "身", "車", "车", "转", "较", "边", "达", "过", "运",
        "近", "还", "这", "进", "连", "适", "选", "通", "速", "造", "道", "那", "部", "都", "酸",
        "采", "里", "重", "量", "金", "铁", "長", "长", "間", "门", "问", "间", "队", "阶", "际",
        "院", "除", "难", "集", "雨", "電", "需", "青", "非", "面", "革", "音", "须", "领", "题",
        "风", "飞", "食", "马", "验", "高", "龙",
    ]
    .iter()
    .copied()
    .collect()
});

pub static KO_NAMES: Lazy<HashSet<&'static str>> =
    Lazy::new(|| ["johab", "cp949", "euc_kr"].iter().copied().collect());
pub static ZH_NAMES: Lazy<HashSet<&'static str>> = Lazy::new(|| {
    ["big5", "cp950", "big5hkscs", "hz"]
        .iter()
        .copied()
        .collect()
});

pub const TRACE: u32 = 5;

pub static CHARDET_CORRESPONDENCE: Lazy<HashMap<&'static str, &'static str>> = Lazy::new(|| {
    [
        ("iso2022_kr", "ISO-2022-KR"),
        ("iso2022_jp", "ISO-2022-JP"),
        ("euc_kr", "EUC-KR"),
        ("tis_620", "TIS-620"),
        ("utf_32", "UTF-32"),
        ("euc_jp", "EUC-JP"),
        ("koi8_r", "KOI8-R"),
        ("iso8859_1", "ISO-8859-1"),
        ("iso8859_2", "ISO-8859-2"),
        ("iso8859_5", "ISO-8859-5"),
        ("iso8859_6", "ISO-8859-6"),
        ("iso8859_7", "ISO-8859-7"),
        ("iso8859_8", "ISO-8859-8"),
        ("utf_16", "UTF-16"),
        ("cp855", "IBM855"),
        ("mac_cyrillic", "MacCyrillic"),
        ("gb2312", "GB2312"),
        ("gb18030", "GB18030"),
        ("cp932", "CP932"),
        ("cp866", "IBM866"),
        ("utf_8", "utf-8"),
        ("utf_8_sig", "UTF-8-SIG"),
        ("shift_jis", "SHIFT_JIS"),
        ("big5", "Big5"),
        ("cp1250", "windows-1250"),
        ("cp1251", "windows-1251"),
        ("cp1252", "Windows-1252"),
        ("cp1253", "windows-1253"),
        ("cp1255", "windows-1255"),
        ("cp1256", "windows-1256"),
        ("cp1254", "Windows-1254"),
        ("cp949", "CP949"),
    ]
    .iter()
    .copied()
    .collect()
});
