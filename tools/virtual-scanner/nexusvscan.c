/*
 * Nexus Virtual Scanner вЂ” a TWAIN 2.4 data source for testing Nexus Optimizer
 * on a computer without a scanner. It behaves like an office MFP: a flatbed
 * plus a document feeder with duplex, colour/grey/black-and-white, 75-600 dpi,
 * native and memory transfers, its own window or none.
 *
 * Every page is drawn on the fly: a header with the page number and side,
 * lines of "text", a colour stamp, paper tone and noise. Test behaviour is set
 * through environment variables, inherited from whoever loads the source:
 *
 *   NEXUS_VSCAN_PAGES=n        sheets in the feeder (default 3)
 *   NEXUS_VSCAN_EMPTY=1        the feeder is empty
 *   NEXUS_VSCAN_JAM_AT=n       paper jam on transfer n
 *   NEXUS_VSCAN_CRASH_AT=n     access violation on transfer n (crash test)
 *   NEXUS_VSCAN_HANG_AT=n      hang forever on transfer n (hang test)
 *   NEXUS_VSCAN_SKEW=deg       pages come out rotated by deg degrees
 *   NEXUS_VSCAN_BLANK_BACKS=1  back sides are blank (dust and noise only)
 *   NEXUS_VSCAN_UI_AUTO=1      the source's own window presses Scan itself
 *   NEXUS_VSCAN_UI_REQUIRED=1  CAP_UICONTROLLABLE = FALSE (window mandatory)
 *   NEXUS_VSCAN_OFFLINE=1      CAP_DEVICEONLINE = FALSE
 *   NEXUS_VSCAN_LEGACY=1       TWAIN 1.9 behaviour: no DF_DS2, notifications
 *                              only through DAT_EVENT
 *   NEXUS_VSCAN_DELAY_MS=n     time to "scan" one page (default 150)
 *   NEXUS_VSCAN_LOG=path       protocol log
 *
 * Test equipment only: never shipped with the program.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "twain_min.h"

/* ------------------------------------------------------------------ state */

static HINSTANCE g_inst;
static TW_IDENTITY g_self;   /* our identity as the DSM numbered it */
static TW_IDENTITY g_app;    /* the application that opened us */
static int g_open;
static int g_state = 3;      /* TWAIN states 4..7 while open */
static TW_ENTRYPOINT g_ep;
static int g_have_ep;
static uint16_t g_cc;        /* condition code for DAT_STATUS */
static HWND g_parent, g_ui;
static int g_show_ui;
static uint16_t g_pending_msg;  /* legacy notification waiting for DAT_EVENT */
static UINT_PTR g_start_timer;

/* environment */
static int e_pages = 3, e_empty, e_jam_at, e_crash_at, e_hang_at;
static double e_skew;
static int e_blank_backs, e_ui_auto, e_ui_required, e_offline, e_legacy;
static int e_delay_ms = 150;
static int e_initial_duplex, e_feeder_sets_duplex, e_paper_sets_duplex, e_refuse_simplex, e_ignore_simplex, e_no_duplex;
static char e_log[MAX_PATH];

/* capabilities */
static int c_pixeltype, c_bitdepth, c_xres, c_yres, c_xfercount;
static int c_feeder, c_duplex, c_autofeed, c_indicators, c_xfermech, c_size;
static double c_frame_w, c_frame_h;  /* explicit DAT_IMAGELAYOUT frame, 0 = by size */

/* job */
static int j_pending;        /* transfers left in the batch */
static int j_done;           /* transfers completed in this session */
static int j_sheet_side;     /* 0 front, 1 back for the current transfer */
static uint8_t *j_img;       /* rendered current page, top-down RGB */
static int j_w, j_h, j_mem_row;

static const int RES_LIST[] = {75, 100, 150, 200, 300, 400, 600};
#define RES_N ((int)(sizeof(RES_LIST) / sizeof(RES_LIST[0])))
static const uint16_t SIZES[] = {TWSS_NONE, TWSS_A4, TWSS_USLETTER, TWSS_USLEGAL};
#define SIZES_N 4
static const uint16_t SUPPORTED[] = {
    CAP_XFERCOUNT, ICAP_COMPRESSION, ICAP_PIXELTYPE, ICAP_UNITS, ICAP_XFERMECH,
    CAP_SUPPORTEDCAPS, CAP_FEEDERENABLED, CAP_FEEDERLOADED, CAP_AUTOFEED,
    CAP_INDICATORS, CAP_PAPERDETECTABLE, CAP_UICONTROLLABLE, CAP_DEVICEONLINE,
    CAP_DUPLEX, CAP_DUPLEXENABLED, ICAP_PHYSICALWIDTH, ICAP_PHYSICALHEIGHT,
    ICAP_XNATIVERESOLUTION, ICAP_YNATIVERESOLUTION, ICAP_XRESOLUTION,
    ICAP_YRESOLUTION, ICAP_PIXELFLAVOR, ICAP_SUPPORTEDSIZES, ICAP_BITDEPTH};
#define SUPPORTED_N ((int)(sizeof(SUPPORTED) / sizeof(SUPPORTED[0])))

/* ---------------------------------------------------------------- helpers */

static void vlog(const char *fmt, ...)
{
    if (!e_log[0]) return;
    FILE *f = fopen(e_log, "a");
    if (!f) return;
    SYSTEMTIME st;
    GetLocalTime(&st);
    fprintf(f, "%02d:%02d:%02d.%03d [%d-bit] ", st.wHour, st.wMinute, st.wSecond,
            st.wMilliseconds, (int)(8 * sizeof(void *)));
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static int env_int(const char *name, int def)
{
    char buf[64];
    DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? atoi(buf) : def;
}

static double env_double(const char *name, double def)
{
    char buf[64];
    DWORD n = GetEnvironmentVariableA(name, buf, sizeof(buf));
    return (n > 0 && n < sizeof(buf)) ? atof(buf) : def;
}

static void read_env(void)
{
    e_pages = env_int("NEXUS_VSCAN_PAGES", 3);
    e_empty = env_int("NEXUS_VSCAN_EMPTY", 0);
    e_jam_at = env_int("NEXUS_VSCAN_JAM_AT", 0);
    e_crash_at = env_int("NEXUS_VSCAN_CRASH_AT", 0);
    e_hang_at = env_int("NEXUS_VSCAN_HANG_AT", 0);
    e_skew = env_double("NEXUS_VSCAN_SKEW", 0.0);
    e_blank_backs = env_int("NEXUS_VSCAN_BLANK_BACKS", 0);
    e_ui_auto = env_int("NEXUS_VSCAN_UI_AUTO", 0);
    e_ui_required = env_int("NEXUS_VSCAN_UI_REQUIRED", 0);
    e_offline = env_int("NEXUS_VSCAN_OFFLINE", 0);
    e_legacy = env_int("NEXUS_VSCAN_LEGACY", 0);
    e_delay_ms = env_int("NEXUS_VSCAN_DELAY_MS", 150);
    e_initial_duplex = env_int("NEXUS_VSCAN_INITIAL_DUPLEX", 0);
    e_feeder_sets_duplex = env_int("NEXUS_VSCAN_FEEDER_SETS_DUPLEX", 0);
    e_paper_sets_duplex = env_int("NEXUS_VSCAN_PAPER_SETS_DUPLEX", 0);
    e_refuse_simplex = env_int("NEXUS_VSCAN_REFUSE_SIMPLEX", 0);
    e_ignore_simplex = env_int("NEXUS_VSCAN_IGNORE_SIMPLEX", 0);
    e_no_duplex = env_int("NEXUS_VSCAN_NO_DUPLEX", 0);
    if (!GetEnvironmentVariableA("NEXUS_VSCAN_LOG", e_log, sizeof(e_log)))
        e_log[0] = 0;
}

static void reset_caps(void)
{
    c_pixeltype = TWPT_RGB;
    c_bitdepth = 8;
    c_xres = c_yres = 200;
    c_xfercount = -1;
    c_feeder = e_initial_duplex ? 1 : 0;
    c_duplex = e_initial_duplex ? 1 : 0;
    c_autofeed = 1;
    c_indicators = 1;
    c_xfermech = TWSX_NATIVE;
    c_size = TWSS_A4;
    c_frame_w = c_frame_h = 0;
}

static double fix_to_double(uint32_t item)
{
    int16_t whole = (int16_t)(item & 0xFFFF);
    uint16_t frac = (uint16_t)(item >> 16);
    return whole + frac / 65536.0;
}

static uint32_t double_to_fix(double v)
{
    int32_t value = (int32_t)(v * 65536.0 + (v < 0 ? -0.5 : 0.5));
    uint16_t whole = (uint16_t)(value >> 16);
    uint16_t frac = (uint16_t)(value & 0xFFFF);
    return ((uint32_t)frac << 16) | whole;
}

static TW_FIX32 make_fix(double v)
{
    uint32_t raw = double_to_fix(v);
    TW_FIX32 f;
    f.Whole = (int16_t)(raw & 0xFFFF);
    f.Frac = (uint16_t)(raw >> 16);
    return f;
}

static HANDLE mem_alloc(uint32_t n)
{
    if (g_have_ep && g_ep.DSM_MemAllocate) return g_ep.DSM_MemAllocate(n);
    return GlobalAlloc(GHND, n);
}

static void *mem_lock(HANDLE h)
{
    if (g_have_ep && g_ep.DSM_MemLock) return g_ep.DSM_MemLock(h);
    return GlobalLock(h);
}

static void mem_unlock(HANDLE h)
{
    if (g_have_ep && g_ep.DSM_MemUnlock) g_ep.DSM_MemUnlock(h);
    else GlobalUnlock(h);
}

static int type_size(uint16_t t)
{
    switch (t) {
    case TWTY_INT8: case TWTY_UINT8: return 1;
    case TWTY_INT16: case TWTY_UINT16: case TWTY_BOOL: return 2;
    default: return 4;
    }
}

static void put_item(uint8_t *dst, uint16_t type, uint32_t v)
{
    memcpy(dst, &v, type_size(type)); /* little endian: low bytes first */
}

static HANDLE make_onevalue(uint16_t type, uint32_t item)
{
    HANDLE h = mem_alloc(sizeof(TW_ONEVALUE));
    TW_ONEVALUE *p = (TW_ONEVALUE *)mem_lock(h);
    p->ItemType = type;
    p->Item = item;
    mem_unlock(h);
    return h;
}

static HANDLE make_enum(uint16_t type, const uint32_t *vals, int n, int cur, int def)
{
    int sz = type_size(type);
    HANDLE h = mem_alloc((uint32_t)(14 + n * sz));
    TW_ENUMERATION *p = (TW_ENUMERATION *)mem_lock(h);
    p->ItemType = type;
    p->NumItems = (uint32_t)n;
    p->CurrentIndex = (uint32_t)cur;
    p->DefaultIndex = (uint32_t)def;
    for (int i = 0; i < n; i++) put_item(p->ItemList + i * sz, type, vals[i]);
    mem_unlock(h);
    return h;
}

static HANDLE make_array(uint16_t type, const uint32_t *vals, int n)
{
    int sz = type_size(type);
    HANDLE h = mem_alloc((uint32_t)(6 + n * sz));
    TW_ARRAY *p = (TW_ARRAY *)mem_lock(h);
    p->ItemType = type;
    p->NumItems = (uint32_t)n;
    for (int i = 0; i < n; i++) put_item(p->ItemList + i * sz, type, vals[i]);
    mem_unlock(h);
    return h;
}

/* The value an application sends with MSG_SET: a ONEVALUE, or the current
 * item of an ENUMERATION. */
static int read_set_value(TW_CAPABILITY *cap, uint16_t *type, uint32_t *value)
{
    if (!cap->hContainer) return 0;
    uint8_t *p = (uint8_t *)mem_lock(cap->hContainer);
    if (!p) return 0;
    int ok = 1;
    if (cap->ConType == TWON_ONEVALUE) {
        TW_ONEVALUE *ov = (TW_ONEVALUE *)p;
        *type = ov->ItemType;
        *value = 0;
        memcpy(value, &ov->Item, type_size(ov->ItemType));
    } else if (cap->ConType == TWON_ENUMERATION) {
        TW_ENUMERATION *en = (TW_ENUMERATION *)p;
        *type = en->ItemType;
        *value = 0;
        if (en->CurrentIndex < en->NumItems)
            memcpy(value, en->ItemList + en->CurrentIndex * type_size(en->ItemType),
                   type_size(en->ItemType));
        else ok = 0;
    } else {
        ok = 0;
    }
    mem_unlock(cap->hContainer);
    if (ok && (*type == TWTY_INT16) && (*value & 0x8000)) *value |= 0xFFFF0000u;
    return ok;
}

static void notify(uint16_t msg)
{
    vlog("notify app: msg 0x%04x (%s)", msg, (g_have_ep && !e_legacy) ? "DSM entry" : "DAT_EVENT");
    if (g_have_ep && !e_legacy && g_ep.DSM_Entry) {
        g_ep.DSM_Entry(&g_self, &g_app, DG_CONTROL, DAT_NULL, msg, NULL);
    } else {
        g_pending_msg = msg;
        if (g_parent) PostMessage(g_parent, WM_NULL, 0, 0);
        else PostThreadMessage(GetCurrentThreadId(), WM_NULL, 0, 0);
    }
}

/* ------------------------------------------------------------- page image */

static double page_w_in(void)
{
    if (c_frame_w > 0) return c_frame_w;
    switch (c_size) {
    case TWSS_USLETTER: return 8.5;
    case TWSS_USLEGAL: return 8.5;
    case TWSS_NONE: return 8.5;
    default: return 8.27;
    }
}

static double page_h_in(void)
{
    if (c_frame_h > 0) return c_frame_h;
    switch (c_size) {
    case TWSS_USLETTER: return 11.0;
    case TWSS_USLEGAL: return 14.0;
    case TWSS_NONE: return 14.0;
    default: return 11.69;
    }
}

static int img_w(void) { return (int)(page_w_in() * c_xres + 0.5); }
static int img_h(void) { return (int)(page_h_in() * c_yres + 0.5); }

static uint32_t hash32(uint32_t x)
{
    x ^= x >> 16; x *= 0x7feb352dU; x ^= x >> 15; x *= 0x846ca68bU; x ^= x >> 16;
    return x;
}

typedef struct { int w, h, xdpi, ydpi; uint8_t *rgb; } Canvas;

static void fill_rect(Canvas *c, double x0, double y0, double x1, double y1,
                      uint8_t r, uint8_t g, uint8_t b)
{
    int px0 = (int)(x0 * c->xdpi), px1 = (int)(x1 * c->xdpi + 0.999);
    int py0 = (int)(y0 * c->ydpi), py1 = (int)(y1 * c->ydpi + 0.999);
    if (px1 <= px0) px1 = px0 + 1;
    if (py1 <= py0) py1 = py0 + 1;
    if (px0 < 0) px0 = 0;
    if (py0 < 0) py0 = 0;
    if (px1 > c->w) px1 = c->w;
    if (py1 > c->h) py1 = c->h;
    for (int y = py0; y < py1; y++) {
        uint8_t *row = c->rgb + (size_t)y * c->w * 3;
        for (int x = px0; x < px1; x++) {
            row[x * 3] = r; row[x * 3 + 1] = g; row[x * 3 + 2] = b;
        }
    }
}

static void ring(Canvas *c, double cx, double cy, double r_out, double r_in,
                 uint8_t r, uint8_t g, uint8_t b)
{
    int x0 = (int)((cx - r_out) * c->xdpi), x1 = (int)((cx + r_out) * c->xdpi) + 1;
    int y0 = (int)((cy - r_out) * c->ydpi), y1 = (int)((cy + r_out) * c->ydpi) + 1;
    for (int y = y0 < 0 ? 0 : y0; y < y1 && y < c->h; y++) {
        for (int x = x0 < 0 ? 0 : x0; x < x1 && x < c->w; x++) {
            double dx = x / (double)c->xdpi - cx, dy = y / (double)c->ydpi - cy;
            double d = sqrt(dx * dx + dy * dy);
            if (d <= r_out && d >= r_in) {
                uint8_t *p = c->rgb + ((size_t)y * c->w + x) * 3;
                p[0] = (uint8_t)((p[0] + 3 * r) / 4);
                p[1] = (uint8_t)((p[1] + 3 * g) / 4);
                p[2] = (uint8_t)((p[2] + 3 * b) / 4);
            }
        }
    }
}

static const uint8_t *glyph(char ch)
{
    static const uint8_t F[][8] = {
        {'A', 0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11}, {'B', 0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E},
        {'C', 0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E}, {'D', 0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C},
        {'E', 0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F}, {'F', 0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10},
        {'G', 0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F}, {'H', 0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11},
        {'I', 0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E}, {'J', 0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C},
        {'K', 0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11}, {'L', 0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F},
        {'M', 0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11}, {'N', 0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11},
        {'O', 0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E}, {'P', 0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10},
        {'Q', 0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D}, {'R', 0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11},
        {'S', 0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E}, {'T', 0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04},
        {'U', 0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E}, {'V', 0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04},
        {'W', 0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A}, {'X', 0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11},
        {'Y', 0x11, 0x11, 0x11, 0x0A, 0x04, 0x04, 0x04}, {'Z', 0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F},
        {'0', 0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E}, {'1', 0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E},
        {'2', 0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F}, {'3', 0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E},
        {'4', 0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02}, {'5', 0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E},
        {'6', 0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E}, {'7', 0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08},
        {'8', 0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E}, {'9', 0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C},
        {'-', 0, 0, 0, 0x1F, 0, 0, 0}, {'/', 0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10},
        {':', 0, 0x0C, 0x0C, 0, 0x0C, 0x0C, 0}, {'.', 0, 0, 0, 0, 0, 0x0C, 0x0C},
    };
    for (size_t i = 0; i < sizeof(F) / sizeof(F[0]); i++)
        if (F[i][0] == (uint8_t)ch) return &F[i][1];
    return NULL;
}

static void text(Canvas *c, const char *s, double x, double y, double h,
                 uint8_t r, uint8_t g, uint8_t b)
{
    double cell = h / 7.0;
    for (; *s; s++, x += cell * 6) {
        const uint8_t *gl = glyph(*s);
        if (!gl) continue;
        for (int row = 0; row < 7; row++)
            for (int col = 0; col < 5; col++)
                if (gl[row] & (0x10 >> col))
                    fill_rect(c, x + col * cell, y + row * cell,
                              x + (col + 1) * cell, y + (row + 1) * cell, r, g, b);
    }
}

/* Draws the page into j_img (top-down RGB, j_w x j_h). */
static int render_page(int number, int back)
{
    free(j_img);
    j_img = NULL;
    j_w = img_w();
    j_h = img_h();
    size_t n = (size_t)j_w * j_h * 3;
    Canvas c = {j_w, j_h, c_xres, c_yres, (uint8_t *)malloc(n)};
    if (!c.rgb) return 0;
    uint32_t seed = (uint32_t)number * 7919u + (uint32_t)back * 104729u;

    /* paper: slightly warm, a little darker towards the bottom, noisy */
    for (int y = 0; y < c.h; y++) {
        uint8_t *row = c.rgb + (size_t)y * c.w * 3;
        int shade = 244 - (y * 7) / c.h;
        for (int x = 0; x < c.w; x++) {
            int noise = (int)(hash32((uint32_t)(x * 73856093) ^ (uint32_t)(y * 19349663) ^ seed) & 15) - 8;
            int r = shade + noise, g = shade - 1 + noise, b = shade - 7 + noise;
            row[x * 3] = (uint8_t)(r < 0 ? 0 : r > 255 ? 255 : r);
            row[x * 3 + 1] = (uint8_t)(g < 0 ? 0 : g > 255 ? 255 : g);
            row[x * 3 + 2] = (uint8_t)(b < 0 ? 0 : b > 255 ? 255 : b);
        }
    }

    double W = page_w_in(), H = page_h_in();
    if (back && e_blank_backs) {
        for (int i = 0; i < 4; i++) {  /* a few dust specks */
            double x = 0.6 + (hash32(seed + i * 31) % 1000) / 1000.0 * (W - 1.2);
            double y = 0.6 + (hash32(seed + i * 57) % 1000) / 1000.0 * (H - 1.2);
            fill_rect(&c, x, y, x + 0.008, y + 0.008, 90, 90, 90);
        }
    } else {
        char line[64];
        text(&c, "NEXUS VIRTUAL SCANNER", 1.0, 0.75, 0.28, 25, 25, 30);
        sprintf_s(line, sizeof(line), "PAGE %d %s", number, back ? "BACK" : "FRONT");
        text(&c, line, 1.0, 1.18, 0.2, 25, 25, 30);
        sprintf_s(line, sizeof(line), "%d DPI %s", c_xres,
                  c_pixeltype == TWPT_BW ? "BW" : c_pixeltype == TWPT_GRAY ? "GRAY" : "COLOR");
        text(&c, line, W - 1.0 - strlen(line) * 0.2 * 6 / 7, 1.18, 0.2, 25, 25, 30);
        fill_rect(&c, 1.0, 1.55, W - 1.0, 1.57, 40, 40, 45);

        uint32_t rnd = seed;
        double y = 1.85;
        int line_no = 0;
        while (y < H - 2.9) {
            double x = 1.0, right = W - 1.0;
            int last_in_par = (line_no % 7) == 6;
            if (last_in_par) right = 1.0 + (W - 2.0) * (0.35 + (hash32(rnd++) % 40) / 100.0);
            while (x < right - 0.2) {
                double word = 0.22 + (hash32(rnd++) % 70) / 100.0;
                if (x + word > right) word = right - x;
                fill_rect(&c, x, y, x + word, y + 0.105, 35, 35, 40);
                x += word + 0.09;
            }
            y += last_in_par ? 0.46 : 0.23;
            line_no++;
        }
        /* a small table, as on a form */
        double ty = H - 2.6;
        for (int k = 0; k <= 3; k++) fill_rect(&c, 1.0, ty + k * 0.3, W - 1.0, ty + k * 0.3 + 0.012, 40, 40, 45);
        fill_rect(&c, 1.0, ty, 1.012, ty + 0.9, 40, 40, 45);
        fill_rect(&c, W / 2, ty, W / 2 + 0.012, ty + 0.9, 40, 40, 45);
        fill_rect(&c, W - 1.012, ty, W - 1.0, ty + 0.9, 40, 40, 45);
        /* blue signature-like stroke and a red stamp */
        for (int i = 0; i < 400; i++) {
            double t = i / 400.0;
            double sx = 1.3 + t * 1.6, sy = ty + 0.62 - 0.12 * sin(t * 18.0) * (1.0 - t);
            fill_rect(&c, sx, sy, sx + 0.025, sy + 0.025, 30, 55, 165);
        }
        ring(&c, W - 2.1, ty + 0.45, 0.55, 0.50, 200, 35, 50);
        ring(&c, W - 2.1, ty + 0.45, 0.40, 0.37, 200, 35, 50);
        text(&c, "NEXUS", W - 2.1 - 0.47, ty + 0.36, 0.17, 200, 35, 50);
        sprintf_s(line, sizeof(line), "- %d -", number);
        text(&c, line, W / 2 - strlen(line) * 0.16 * 6 / 7 / 2, H - 0.8, 0.16, 60, 60, 65);
    }

    if (e_skew != 0.0) {  /* rotate the whole sheet around its centre */
        uint8_t *out = (uint8_t *)malloc(n);
        if (out) {
            double a = e_skew * 3.14159265358979 / 180.0, ca = cos(a), sa = sin(a);
            double cx = c.w / 2.0, cy = c.h / 2.0;
            for (int y = 0; y < c.h; y++) {
                uint8_t *row = out + (size_t)y * c.w * 3;
                for (int x = 0; x < c.w; x++) {
                    double dx = x - cx, dy = y - cy;
                    int sx = (int)(ca * dx + sa * dy + cx), sy = (int)(-sa * dx + ca * dy + cy);
                    if (sx >= 0 && sy >= 0 && sx < c.w && sy < c.h) {
                        memcpy(row + x * 3, c.rgb + ((size_t)sy * c.w + sx) * 3, 3);
                    } else {
                        row[x * 3] = 236; row[x * 3 + 1] = 235; row[x * 3 + 2] = 230;
                    }
                }
            }
            free(c.rgb);
            c.rgb = out;
        }
    }
    j_img = c.rgb;
    j_mem_row = 0;
    return 1;
}

static int luminance(const uint8_t *p) { return (p[0] * 299 + p[1] * 587 + p[2] * 114) / 1000; }

static int bytes_per_row_mem(void)
{
    if (c_pixeltype == TWPT_BW) return (j_w + 7) / 8;
    if (c_pixeltype == TWPT_GRAY) return j_w;
    return j_w * 3;
}

/* One top-down row in TWAIN memory-transfer layout (RGB order; for
 * black-and-white and grey TWPF_CHOCOLATE: zero is black). */
static void mem_row(int y, uint8_t *dst)
{
    const uint8_t *src = j_img + (size_t)y * j_w * 3;
    if (c_pixeltype == TWPT_RGB) {
        memcpy(dst, src, (size_t)j_w * 3);
    } else if (c_pixeltype == TWPT_GRAY) {
        for (int x = 0; x < j_w; x++) dst[x] = (uint8_t)luminance(src + x * 3);
    } else {
        memset(dst, 0, (j_w + 7) / 8);
        for (int x = 0; x < j_w; x++)
            if (luminance(src + x * 3) >= 150) dst[x >> 3] |= (uint8_t)(0x80 >> (x & 7));
    }
}

/* The current page as a packed DIB in a global handle (native transfer). */
static HANDLE make_dib(void)
{
    int bits = c_pixeltype == TWPT_BW ? 1 : c_pixeltype == TWPT_GRAY ? 8 : 24;
    int colors = bits == 1 ? 2 : bits == 8 ? 256 : 0;
    int stride = ((j_w * bits + 31) / 32) * 4;
    size_t size = sizeof(BITMAPINFOHEADER) + colors * 4 + (size_t)stride * j_h;
    HANDLE h = GlobalAlloc(GHND, size);
    if (!h) return NULL;
    uint8_t *p = (uint8_t *)GlobalLock(h);
    BITMAPINFOHEADER *bih = (BITMAPINFOHEADER *)p;
    bih->biSize = sizeof(BITMAPINFOHEADER);
    bih->biWidth = j_w;
    bih->biHeight = j_h;  /* bottom-up */
    bih->biPlanes = 1;
    bih->biBitCount = (WORD)bits;
    bih->biCompression = BI_RGB;
    bih->biSizeImage = (DWORD)stride * j_h;
    bih->biXPelsPerMeter = (LONG)(c_xres * 39.3701 + 0.5);
    bih->biYPelsPerMeter = (LONG)(c_yres * 39.3701 + 0.5);
    bih->biClrUsed = (DWORD)colors;
    RGBQUAD *pal = (RGBQUAD *)(p + sizeof(BITMAPINFOHEADER));
    for (int i = 0; i < colors; i++) {
        uint8_t v = (uint8_t)(colors == 2 ? i * 255 : i);
        pal[i].rgbRed = pal[i].rgbGreen = pal[i].rgbBlue = v;
        pal[i].rgbReserved = 0;
    }
    uint8_t *bitsp = p + sizeof(BITMAPINFOHEADER) + colors * 4;
    for (int y = 0; y < j_h; y++) {
        uint8_t *dst = bitsp + (size_t)(j_h - 1 - y) * stride;
        const uint8_t *src = j_img + (size_t)y * j_w * 3;
        if (bits == 24) {
            for (int x = 0; x < j_w; x++) {
                dst[x * 3] = src[x * 3 + 2];
                dst[x * 3 + 1] = src[x * 3 + 1];
                dst[x * 3 + 2] = src[x * 3];
            }
        } else if (bits == 8) {
            for (int x = 0; x < j_w; x++) dst[x] = (uint8_t)luminance(src + x * 3);
        } else {
            for (int x = 0; x < j_w; x++)
                if (luminance(src + x * 3) >= 150) dst[x >> 3] |= (uint8_t)(0x80 >> (x & 7));
        }
    }
    GlobalUnlock(h);
    return h;
}

/* ----------------------------------------------------------------- the job */

static void start_batch(void)
{
    if (c_feeder) {
        int sides = c_duplex ? 2 : 1;
        j_pending = e_empty ? 0 : e_pages * sides;
        if (c_xfercount > 0 && c_xfercount < j_pending) j_pending = c_xfercount;
    } else {
        j_pending = 1;
    }
    vlog("batch: %d transfer(s), feeder=%d duplex=%d res=%d type=%d mech=%d",
         j_pending, c_feeder, c_duplex, c_xres, c_pixeltype, c_xfermech);
}

/* Called before a transfer: applies the test faults. 0 = go ahead. */
static int before_transfer(void)
{
    int number = j_done + 1;
    if (e_crash_at && number == e_crash_at) {
        vlog("simulated crash on transfer %d", number);
        volatile int *bad = NULL;
        *bad = 42;
    }
    if (e_hang_at && number == e_hang_at) {
        vlog("simulated hang on transfer %d", number);
        for (;;) Sleep(1000);
    }
    if (e_jam_at && number == e_jam_at) {
        vlog("simulated paper jam on transfer %d", number);
        g_cc = TWCC_PAPERJAM;
        return 1;
    }
    if (e_delay_ms > 0) Sleep((DWORD)e_delay_ms);
    return 0;
}

static void current_sheet(int *number, int *back)
{
    if (c_feeder && c_duplex) {
        *number = j_done / 2 + 1;
        *back = j_done % 2;
    } else {
        *number = j_done + 1;
        *back = 0;
    }
}

/* -------------------------------------------------------------------- UI */

#define ID_SCAN 1001
#define ID_CANCEL 1002
#define TIMER_AUTO 77

static LRESULT CALLBACK ui_proc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    switch (m) {
    case WM_CREATE: {
        HFONT font = (HFONT)GetStockObject(DEFAULT_GUI_FONT);
        char info[256];
        sprintf_s(info, sizeof(info),
                  "Nexus Virtual Scanner - test device\r\n\r\n"
                  "Source: %s%s\r\nMode: %s, %d dpi\r\nSheets in the feeder: %d",
                  c_feeder ? "document feeder" : "flatbed",
                  c_feeder && c_duplex ? " (both sides)" : "",
                  c_pixeltype == TWPT_BW ? "black and white" :
                  c_pixeltype == TWPT_GRAY ? "greyscale" : "colour",
                  c_xres, e_empty ? 0 : e_pages);
        HWND s = CreateWindowExA(0, "STATIC", info, WS_CHILD | WS_VISIBLE, 16, 14, 380, 96, h, NULL, g_inst, NULL);
        HWND b1 = CreateWindowExA(0, "BUTTON", "Scan", WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_DEFPUSHBUTTON,
                                  196, 122, 96, 30, h, (HMENU)ID_SCAN, g_inst, NULL);
        HWND b2 = CreateWindowExA(0, "BUTTON", "Cancel", WS_CHILD | WS_VISIBLE | WS_TABSTOP,
                                  300, 122, 96, 30, h, (HMENU)ID_CANCEL, g_inst, NULL);
        SendMessage(s, WM_SETFONT, (WPARAM)font, TRUE);
        SendMessage(b1, WM_SETFONT, (WPARAM)font, TRUE);
        SendMessage(b2, WM_SETFONT, (WPARAM)font, TRUE);
        if (e_ui_auto) SetTimer(h, TIMER_AUTO, 400, NULL);
        return 0;
    }
    case WM_TIMER:
        if (w == TIMER_AUTO) {
            KillTimer(h, TIMER_AUTO);
            PostMessage(h, WM_COMMAND, ID_SCAN, 0);
        }
        return 0;
    case WM_COMMAND:
        if (LOWORD(w) == ID_SCAN && g_state == 5) {
            start_batch();
            if (j_pending == 0) {
                MessageBoxA(h, "The document feeder is empty.", "Nexus Virtual Scanner", MB_ICONWARNING);
                return 0;
            }
            g_state = 6;
            notify(MSG_XFERREADY);
        } else if (LOWORD(w) == ID_CANCEL) {
            notify(MSG_CLOSEDSREQ);
        }
        return 0;
    case WM_CLOSE:
        notify(MSG_CLOSEDSREQ);
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

static void show_ui(void)
{
    static ATOM cls;
    if (!cls) {
        WNDCLASSA wc = {0};
        wc.lpfnWndProc = ui_proc;
        wc.hInstance = g_inst;
        wc.hCursor = LoadCursor(NULL, IDC_ARROW);
        wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
        wc.lpszClassName = "NexusVirtualScannerUI";
        cls = RegisterClassA(&wc);
    }
    RECT r = {0, 0, 420, 172};
    AdjustWindowRect(&r, WS_CAPTION | WS_SYSMENU, FALSE);
    int ww = r.right - r.left, wh = r.bottom - r.top;
    int x = (GetSystemMetrics(SM_CXSCREEN) - ww) / 2, y = (GetSystemMetrics(SM_CYSCREEN) - wh) / 2;
    g_ui = CreateWindowExA(WS_EX_DLGMODALFRAME, "NexusVirtualScannerUI", "Nexus Virtual Scanner",
                           WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_VISIBLE, x, y, ww, wh,
                           g_parent, NULL, g_inst, NULL);
    if (g_ui) SetForegroundWindow(g_ui);
}

static void CALLBACK start_timer_proc(HWND h, UINT m, UINT_PTR id, DWORD t)
{
    (void)h; (void)m; (void)t;
    KillTimer(NULL, id);
    g_start_timer = 0;
    if (g_state != 5) return;
    start_batch();
    g_state = 6;
    notify(MSG_XFERREADY);
}

/* ---------------------------------------------------------- capabilities */

static int res_index(int dpi)
{
    for (int i = 0; i < RES_N; i++) if (RES_LIST[i] == dpi) return i;
    return -1;
}

static uint16_t cap_get(TW_CAPABILITY *cap, uint16_t msg)
{
    uint32_t v[32];
    int cur_only = (msg == MSG_GETCURRENT || msg == MSG_GETDEFAULT || msg == MSG_RESET);
    int def = msg == MSG_GETDEFAULT;
    HANDLE h = NULL;
    switch (cap->Cap) {
    case CAP_XFERCOUNT:
        h = make_onevalue(TWTY_INT16, (uint32_t)(uint16_t)(def ? -1 : c_xfercount));
        cap->ConType = TWON_ONEVALUE;
        break;
    case ICAP_COMPRESSION:
    case ICAP_UNITS:
    case ICAP_PIXELFLAVOR:
        v[0] = 0;
        if (cur_only) { h = make_onevalue(TWTY_UINT16, 0); cap->ConType = TWON_ONEVALUE; }
        else { h = make_enum(TWTY_UINT16, v, 1, 0, 0); cap->ConType = TWON_ENUMERATION; }
        break;
    case ICAP_PIXELTYPE:
        if (cur_only) { h = make_onevalue(TWTY_UINT16, def ? TWPT_RGB : c_pixeltype); cap->ConType = TWON_ONEVALUE; }
        else { v[0] = TWPT_BW; v[1] = TWPT_GRAY; v[2] = TWPT_RGB; h = make_enum(TWTY_UINT16, v, 3, c_pixeltype, 2); cap->ConType = TWON_ENUMERATION; }
        break;
    case ICAP_BITDEPTH: {
        int bd = c_pixeltype == TWPT_BW ? 1 : 8;
        if (cur_only) { h = make_onevalue(TWTY_UINT16, bd); cap->ConType = TWON_ONEVALUE; }
        else { v[0] = (uint32_t)bd; h = make_enum(TWTY_UINT16, v, 1, 0, 0); cap->ConType = TWON_ENUMERATION; }
        break;
    }
    case ICAP_XFERMECH:
        if (cur_only) { h = make_onevalue(TWTY_UINT16, def ? TWSX_NATIVE : c_xfermech); cap->ConType = TWON_ONEVALUE; }
        else { v[0] = TWSX_NATIVE; v[1] = TWSX_MEMORY; h = make_enum(TWTY_UINT16, v, 2, c_xfermech == TWSX_MEMORY, 0); cap->ConType = TWON_ENUMERATION; }
        break;
    case ICAP_XRESOLUTION:
    case ICAP_YRESOLUTION: {
        int cur = cap->Cap == ICAP_XRESOLUTION ? c_xres : c_yres;
        if (cur_only) { h = make_onevalue(TWTY_FIX32, double_to_fix(def ? 200 : cur)); cap->ConType = TWON_ONEVALUE; }
        else {
            for (int i = 0; i < RES_N; i++) v[i] = double_to_fix(RES_LIST[i]);
            h = make_enum(TWTY_FIX32, v, RES_N, res_index(cur), res_index(200));
            cap->ConType = TWON_ENUMERATION;
        }
        break;
    }
    case ICAP_XNATIVERESOLUTION:
    case ICAP_YNATIVERESOLUTION:
        h = make_onevalue(TWTY_FIX32, double_to_fix(600));
        cap->ConType = TWON_ONEVALUE;
        break;
    case ICAP_PHYSICALWIDTH:
        h = make_onevalue(TWTY_FIX32, double_to_fix(8.5));
        cap->ConType = TWON_ONEVALUE;
        break;
    case ICAP_PHYSICALHEIGHT:
        h = make_onevalue(TWTY_FIX32, double_to_fix(14.0));
        cap->ConType = TWON_ONEVALUE;
        break;
    case ICAP_SUPPORTEDSIZES: {
        int cur = 0;
        for (int i = 0; i < SIZES_N; i++) if (SIZES[i] == c_size) cur = i;
        if (cur_only) { h = make_onevalue(TWTY_UINT16, def ? TWSS_A4 : c_size); cap->ConType = TWON_ONEVALUE; }
        else { for (int i = 0; i < SIZES_N; i++) v[i] = SIZES[i]; h = make_enum(TWTY_UINT16, v, SIZES_N, cur, 1); cap->ConType = TWON_ENUMERATION; }
        break;
    }
    case CAP_SUPPORTEDCAPS:
        for (int i = 0; i < SUPPORTED_N; i++) v[i] = SUPPORTED[i];
        h = make_array(TWTY_UINT16, v, SUPPORTED_N);
        cap->ConType = TWON_ARRAY;
        break;
    case CAP_FEEDERENABLED: h = make_onevalue(TWTY_BOOL, def ? 0 : c_feeder); cap->ConType = TWON_ONEVALUE; break;
    case CAP_DUPLEXENABLED: h = make_onevalue(TWTY_BOOL, def ? 0 : c_duplex); cap->ConType = TWON_ONEVALUE; break;
    case CAP_AUTOFEED: h = make_onevalue(TWTY_BOOL, def ? 1 : c_autofeed); cap->ConType = TWON_ONEVALUE; break;
    case CAP_INDICATORS: h = make_onevalue(TWTY_BOOL, def ? 1 : c_indicators); cap->ConType = TWON_ONEVALUE; break;
    case CAP_FEEDERLOADED: h = make_onevalue(TWTY_BOOL, (!e_empty && e_pages > 0) ? 1 : 0); cap->ConType = TWON_ONEVALUE; break;
    case CAP_PAPERDETECTABLE: h = make_onevalue(TWTY_BOOL, 1); cap->ConType = TWON_ONEVALUE; break;
    case CAP_UICONTROLLABLE: h = make_onevalue(TWTY_BOOL, e_ui_required ? 0 : 1); cap->ConType = TWON_ONEVALUE; break;
    case CAP_DEVICEONLINE: h = make_onevalue(TWTY_BOOL, e_offline ? 0 : 1); cap->ConType = TWON_ONEVALUE; break;
    case CAP_DUPLEX: h = make_onevalue(TWTY_UINT16, e_no_duplex ? 0 : TWDX_1PASSDUPLEX); cap->ConType = TWON_ONEVALUE; break;
    default:
        g_cc = TWCC_CAPUNSUPPORTED;
        return TWRC_FAILURE;
    }
    if (!h) { g_cc = TWCC_LOWMEMORY; return TWRC_FAILURE; }
    cap->hContainer = h;
    return TWRC_SUCCESS;
}

static int cap_is_readonly(uint16_t id)
{
    switch (id) {
    case CAP_FEEDERLOADED: case CAP_PAPERDETECTABLE: case CAP_UICONTROLLABLE:
    case CAP_DEVICEONLINE: case CAP_DUPLEX: case CAP_SUPPORTEDCAPS:
    case ICAP_PHYSICALWIDTH: case ICAP_PHYSICALHEIGHT:
    case ICAP_XNATIVERESOLUTION: case ICAP_YNATIVERESOLUTION:
        return 1;
    }
    return 0;
}

static uint16_t cap_set(TW_CAPABILITY *cap)
{
    if (g_state != 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
    int known = 0;
    for (int i = 0; i < SUPPORTED_N; i++) if (SUPPORTED[i] == cap->Cap) known = 1;
    if (!known) { g_cc = TWCC_CAPUNSUPPORTED; return TWRC_FAILURE; }
    if (cap_is_readonly(cap->Cap)) { g_cc = TWCC_CAPBADOPERATION; return TWRC_FAILURE; }
    uint16_t type;
    uint32_t v;
    if (!read_set_value(cap, &type, &v)) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
    switch (cap->Cap) {
    case CAP_XFERCOUNT: {
        int n = (int)(int16_t)(v & 0xFFFF);
        if (n == 0 || n < -1) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        c_xfercount = n;
        break;
    }
    case ICAP_COMPRESSION: case ICAP_UNITS: case ICAP_PIXELFLAVOR:
        if (v != 0) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        break;
    case ICAP_PIXELTYPE:
        if (v > TWPT_RGB) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        c_pixeltype = (int)v;
        c_bitdepth = v == TWPT_BW ? 1 : 8;
        break;
    case ICAP_BITDEPTH:
        if ((int)v != (c_pixeltype == TWPT_BW ? 1 : 8)) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        break;
    case ICAP_XFERMECH:
        if (v != TWSX_NATIVE && v != TWSX_MEMORY) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        c_xfermech = (int)v;
        break;
    case ICAP_XRESOLUTION:
    case ICAP_YRESOLUTION: {
        double d = type == TWTY_FIX32 ? fix_to_double(v) : (double)v;
        int dpi = (int)(d + 0.5);
        if (res_index(dpi) < 0) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        if (cap->Cap == ICAP_XRESOLUTION) c_xres = dpi; else c_yres = dpi;
        break;
    }
    case ICAP_SUPPORTEDSIZES: {
        int ok = 0;
        for (int i = 0; i < SIZES_N; i++) if (SIZES[i] == v) ok = 1;
        if (!ok) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        c_size = (int)v;
        if (c_feeder && e_paper_sets_duplex && !e_no_duplex) c_duplex = 1;
        c_frame_w = c_frame_h = 0;
        break;
    }
    case CAP_FEEDERENABLED:
        c_feeder = v ? 1 : 0;
        if (!c_feeder) c_duplex = 0;
        else if (e_feeder_sets_duplex && !e_no_duplex) c_duplex = 1;
        break;
    case CAP_DUPLEXENABLED:
        if (e_no_duplex || (!v && e_refuse_simplex)) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
        if (!v && e_ignore_simplex) break;
        c_duplex = v ? 1 : 0;
        break;
    case CAP_AUTOFEED: c_autofeed = v ? 1 : 0; break;
    case CAP_INDICATORS: c_indicators = v ? 1 : 0; break;
    default: g_cc = TWCC_CAPBADOPERATION; return TWRC_FAILURE;
    }
    vlog("cap 0x%04x set to %lu", cap->Cap, (unsigned long)v);
    return TWRC_SUCCESS;
}

static uint16_t do_capability(uint16_t msg, void *data)
{
    if (msg == MSG_RESETALL) {
        if (g_state != 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
        reset_caps();
        return TWRC_SUCCESS;
    }
    TW_CAPABILITY *cap = (TW_CAPABILITY *)data;
    if (!cap) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
    switch (msg) {
    case MSG_GET:
    case MSG_GETCURRENT:
    case MSG_GETDEFAULT:
        return cap_get(cap, msg);
    case MSG_SET:
        return cap_set(cap);
    case MSG_RESET: {
        if (g_state != 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
        uint16_t id = cap->Cap;
        switch (id) {
        case CAP_XFERCOUNT: c_xfercount = -1; break;
        case ICAP_PIXELTYPE: c_pixeltype = TWPT_RGB; c_bitdepth = 8; break;
        case ICAP_XFERMECH: c_xfermech = TWSX_NATIVE; break;
        case ICAP_XRESOLUTION: c_xres = 200; break;
        case ICAP_YRESOLUTION: c_yres = 200; break;
        case ICAP_SUPPORTEDSIZES: c_size = TWSS_A4; break;
        case CAP_FEEDERENABLED: c_feeder = 0; c_duplex = 0; break;
        case CAP_DUPLEXENABLED: c_duplex = 0; break;
        case CAP_AUTOFEED: c_autofeed = 1; break;
        case CAP_INDICATORS: c_indicators = 1; break;
        default: break;
        }
        return cap_get(cap, MSG_GETCURRENT);
    }
    case MSG_QUERYSUPPORT: {
        int known = 0;
        for (int i = 0; i < SUPPORTED_N; i++) if (SUPPORTED[i] == cap->Cap) known = 1;
        uint32_t flags = 0;
        if (known) {
            flags = TWQC_GET | TWQC_GETCURRENT | TWQC_GETDEFAULT;
            if (!cap_is_readonly(cap->Cap)) flags |= TWQC_SET | TWQC_RESET;
        }
        cap->ConType = TWON_ONEVALUE;
        cap->hContainer = make_onevalue(TWTY_INT32, flags);
        return TWRC_SUCCESS;
    }
    }
    g_cc = TWCC_BADPROTOCOL;
    return TWRC_FAILURE;
}

/* ------------------------------------------------------------ the triplets */

static void close_session(void)
{
    if (g_start_timer) { KillTimer(NULL, g_start_timer); g_start_timer = 0; }
    if (g_ui) { DestroyWindow(g_ui); g_ui = NULL; }
    free(j_img);
    j_img = NULL;
    j_pending = 0;
    g_pending_msg = 0;
}

static uint16_t do_control(TW_IDENTITY *origin, uint16_t dat, uint16_t msg, void *data)
{
    switch (dat) {
    case DAT_IDENTITY:
        if (msg == MSG_GET) {
            TW_IDENTITY *id = (TW_IDENTITY *)data;
            uint32_t keep = id->Id;
            read_env();
            memset(id, 0, sizeof(*id));
            id->Id = keep;
            id->Version.MajorNum = 1;
            id->Version.MinorNum = 0;
            id->Version.Language = 13;  /* TWLG_USA */
            id->Version.Country = 1;    /* TWCY_USA */
            strcpy_s(id->Version.Info, sizeof(id->Version.Info), "1.0 test device");
            id->ProtocolMajor = e_legacy ? 1 : 2;
            id->ProtocolMinor = e_legacy ? 9 : 4;
            id->SupportedGroups = DG_CONTROL | DG_IMAGE | (e_legacy ? 0 : DF_DS2);
            strcpy_s(id->Manufacturer, sizeof(id->Manufacturer), "Nexus Test Lab");
            strcpy_s(id->ProductFamily, sizeof(id->ProductFamily), "Virtual");
            strcpy_s(id->ProductName, sizeof(id->ProductName), "Nexus Virtual Scanner");
            return TWRC_SUCCESS;
        }
        if (msg == MSG_OPENDS) {
            if (g_open) { g_cc = TWCC_MAXCONNECTIONS; return TWRC_FAILURE; }
            read_env();
            g_self = *(TW_IDENTITY *)data;
            g_app = *origin;
            g_open = 1;
            g_state = 4;
            j_done = 0;
            reset_caps();
            vlog("opened by '%s' (protocol %d.%d, groups 0x%08lx), entry points %s",
                 g_app.ProductName, g_app.ProtocolMajor, g_app.ProtocolMinor,
                 (unsigned long)g_app.SupportedGroups, g_have_ep ? "yes" : "no");
            return TWRC_SUCCESS;
        }
        if (msg == MSG_CLOSEDS) {
            if (g_state > 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            close_session();
            g_open = 0;
            g_state = 3;
            g_have_ep = 0;
            vlog("closed");
            return TWRC_SUCCESS;
        }
        break;

    case DAT_ENTRYPOINT:
        if (msg == MSG_SET && data) {
            TW_ENTRYPOINT *ep = (TW_ENTRYPOINT *)data;
            memset(&g_ep, 0, sizeof(g_ep));
            memcpy(&g_ep, ep, ep->Size < sizeof(g_ep) ? ep->Size : sizeof(g_ep));
            g_have_ep = 1;
            return TWRC_SUCCESS;
        }
        break;

    case DAT_STATUS:
        if (msg == MSG_GET) {
            TW_STATUS *st = (TW_STATUS *)data;
            st->ConditionCode = g_cc;
            st->Data = 0;
            g_cc = TWCC_SUCCESS;
            return TWRC_SUCCESS;
        }
        break;

    case DAT_CAPABILITY:
        return do_capability(msg, data);

    case DAT_XFERGROUP:
        if (msg == MSG_GET) { *(uint32_t *)data = DG_IMAGE; return TWRC_SUCCESS; }
        break;

    case DAT_USERINTERFACE:
        if (msg == MSG_ENABLEDS) {
            TW_USERINTERFACE *ui = (TW_USERINTERFACE *)data;
            if (g_state != 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            if (e_offline) { g_cc = TWCC_CHECKDEVICEONLINE; return TWRC_FAILURE; }
            g_parent = (HWND)ui->hParent;
            g_show_ui = ui->ShowUI || e_ui_required;
            uint16_t rc = TWRC_SUCCESS;
            if (!ui->ShowUI && e_ui_required) { ui->ShowUI = 1; rc = TWRC_CHECKSTATUS; }
            if (!g_show_ui && c_feeder && e_empty) { g_cc = TWCC_NOMEDIA; return TWRC_FAILURE; }
            g_state = 5;
            vlog("enabled, ShowUI=%d", g_show_ui);
            if (g_show_ui) show_ui();
            else g_start_timer = SetTimer(NULL, 0, 120, start_timer_proc);
            return rc;
        }
        if (msg == MSG_DISABLEDS) {
            if (g_state != 5) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            close_session();
            g_state = 4;
            vlog("disabled");
            return TWRC_SUCCESS;
        }
        if (msg == MSG_ENABLEDSUIONLY) { g_cc = TWCC_BADPROTOCOL; return TWRC_FAILURE; }
        break;

    case DAT_EVENT:
        if (msg == MSG_PROCESSEVENT) {
            TW_EVENT *ev = (TW_EVENT *)data;
            MSG *m = (MSG *)ev->pEvent;
            ev->TWMessage = MSG_NULL;
            if (g_pending_msg && m && m->message == WM_NULL) {
                ev->TWMessage = g_pending_msg;
                g_pending_msg = 0;
                return TWRC_DSEVENT;
            }
            return TWRC_NOTDSEVENT;
        }
        break;

    case DAT_PENDINGXFERS: {
        TW_PENDINGXFERS *px = (TW_PENDINGXFERS *)data;
        if (msg == MSG_GET) { px->Count = (uint16_t)j_pending; px->EOJ = 0; return TWRC_SUCCESS; }
        if (msg == MSG_ENDXFER) {
            if (g_state != 6 && g_state != 7) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            if (g_state == 7) j_done++;
            if (j_pending > 0) j_pending--;
            free(j_img);
            j_img = NULL;
            px->Count = (uint16_t)j_pending;
            px->EOJ = 0;
            g_state = j_pending > 0 ? 6 : 5;
            vlog("end of transfer, %d left", j_pending);
            return TWRC_SUCCESS;
        }
        if (msg == MSG_RESET) {
            if (g_state != 6 && g_state != 7) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            j_pending = 0;
            free(j_img);
            j_img = NULL;
            px->Count = 0;
            g_state = 5;
            vlog("pending transfers reset");
            return TWRC_SUCCESS;
        }
        break;
    }

    case DAT_SETUPMEMXFER:
        if (msg == MSG_GET) {
            TW_SETUPMEMXFER *s = (TW_SETUPMEMXFER *)data;
            int w = img_w();
            uint32_t row = c_pixeltype == TWPT_BW ? (uint32_t)((w + 7) / 8)
                         : c_pixeltype == TWPT_GRAY ? (uint32_t)w : (uint32_t)w * 3;
            s->MinBufSize = row;
            s->MaxBufSize = 1u << 20;
            s->Preferred = 256u * 1024u > row ? 256u * 1024u : row;
            return TWRC_SUCCESS;
        }
        break;
    }
    g_cc = TWCC_BADPROTOCOL;
    return TWRC_FAILURE;
}

static uint16_t do_image(uint16_t dat, uint16_t msg, void *data)
{
    switch (dat) {
    case DAT_IMAGEINFO:
        if (msg == MSG_GET) {
            if (g_state != 6 && g_state != 7) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            TW_IMAGEINFO *ii = (TW_IMAGEINFO *)data;
            memset(ii, 0, sizeof(*ii));
            ii->XResolution = make_fix(c_xres);
            ii->YResolution = make_fix(c_yres);
            ii->ImageWidth = img_w();
            ii->ImageLength = img_h();
            ii->SamplesPerPixel = c_pixeltype == TWPT_RGB ? 3 : 1;
            for (int i = 0; i < ii->SamplesPerPixel; i++) ii->BitsPerSample[i] = c_pixeltype == TWPT_BW ? 1 : 8;
            ii->BitsPerPixel = c_pixeltype == TWPT_BW ? 1 : c_pixeltype == TWPT_GRAY ? 8 : 24;
            ii->PixelType = (int16_t)c_pixeltype;
            ii->Compression = TWCP_NONE;
            return TWRC_SUCCESS;
        }
        break;

    case DAT_IMAGELAYOUT: {
        TW_IMAGELAYOUT *lay = (TW_IMAGELAYOUT *)data;
        if (msg == MSG_GET || msg == MSG_GETDEFAULT || msg == MSG_GETCURRENT) {
            memset(lay, 0, sizeof(*lay));
            lay->Frame.Right = make_fix(page_w_in());
            lay->Frame.Bottom = make_fix(page_h_in());
            lay->DocumentNumber = lay->PageNumber = lay->FrameNumber = 1;
            return TWRC_SUCCESS;
        }
        if (msg == MSG_SET) {
            if (g_state != 4) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            double w = lay->Frame.Right.Whole + lay->Frame.Right.Frac / 65536.0
                     - lay->Frame.Left.Whole - lay->Frame.Left.Frac / 65536.0;
            double h = lay->Frame.Bottom.Whole + lay->Frame.Bottom.Frac / 65536.0
                     - lay->Frame.Top.Whole - lay->Frame.Top.Frac / 65536.0;
            if (w < 1 || h < 1 || w > 8.5 || h > 14.0) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
            c_frame_w = w;
            c_frame_h = h;
            return TWRC_SUCCESS;
        }
        if (msg == MSG_RESET) { c_frame_w = c_frame_h = 0; return TWRC_SUCCESS; }
        break;
    }

    case DAT_IMAGENATIVEXFER:
        if (msg == MSG_GET) {
            if (g_state != 6) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            if (before_transfer()) return TWRC_FAILURE;
            int number, back;
            current_sheet(&number, &back);
            if (!render_page(number, back)) { g_cc = TWCC_LOWMEMORY; return TWRC_FAILURE; }
            HANDLE dib = make_dib();
            free(j_img);
            j_img = NULL;
            if (!dib) { g_cc = TWCC_LOWMEMORY; return TWRC_FAILURE; }
            *(HANDLE *)data = dib;
            g_state = 7;
            vlog("native transfer %d done (%dx%d)", j_done + 1, j_w, j_h);
            return TWRC_XFERDONE;
        }
        break;

    case DAT_IMAGEMEMXFER:
        if (msg == MSG_GET) {
            if (g_state != 6 && g_state != 7) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
            TW_IMAGEMEMXFER *mx = (TW_IMAGEMEMXFER *)data;
            if (g_state == 6) {
                if (before_transfer()) return TWRC_FAILURE;
                int number, back;
                current_sheet(&number, &back);
                if (!render_page(number, back)) { g_cc = TWCC_LOWMEMORY; return TWRC_FAILURE; }
                g_state = 7;
            }
            int bpr = bytes_per_row_mem();
            uint8_t *dst = (uint8_t *)mx->Memory.TheMem;
            if (!dst || mx->Memory.Length < (uint32_t)bpr) { g_cc = TWCC_BADVALUE; return TWRC_FAILURE; }
            if (mx->Memory.Flags & 0x0010) dst = (uint8_t *)GlobalLock((HANDLE)mx->Memory.TheMem);
            int rows = (int)(mx->Memory.Length / (uint32_t)bpr);
            if (rows > j_h - j_mem_row) rows = j_h - j_mem_row;
            for (int i = 0; i < rows; i++) mem_row(j_mem_row + i, dst + (size_t)i * bpr);
            if (mx->Memory.Flags & 0x0010) GlobalUnlock((HANDLE)mx->Memory.TheMem);
            mx->Compression = TWCP_NONE;
            mx->BytesPerRow = (uint32_t)bpr;
            mx->Columns = (uint32_t)j_w;
            mx->Rows = (uint32_t)rows;
            mx->XOffset = 0;
            mx->YOffset = (uint32_t)j_mem_row;
            mx->BytesWritten = (uint32_t)(rows * bpr);
            j_mem_row += rows;
            if (j_mem_row >= j_h) {
                vlog("memory transfer %d done (%dx%d)", j_done + 1, j_w, j_h);
                return TWRC_XFERDONE;
            }
            return TWRC_SUCCESS;
        }
        break;
    }
    g_cc = TWCC_BADPROTOCOL;
    return TWRC_FAILURE;
}

/* ------------------------------------------------------------ entry point */

uint16_t WINAPI DS_Entry(TW_IDENTITY *origin, uint32_t dg, uint16_t dat,
                                               uint16_t msg, void *data)
{
    if (dat != DAT_EVENT)
        vlog("DS_Entry dg=0x%lx dat=0x%04x msg=0x%04x state=%d", (unsigned long)dg, dat, msg, g_state);
    if ((dg & 0xFFFF) == DG_CONTROL) return do_control(origin, dat, msg, data);
    if ((dg & 0xFFFF) == DG_IMAGE) {
        if (!g_open) { g_cc = TWCC_SEQERROR; return TWRC_FAILURE; }
        return do_image(dat, msg, data);
    }
    g_cc = TWCC_BADPROTOCOL;
    return TWRC_FAILURE;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_inst = inst;
        DisableThreadLibraryCalls(inst);
    }
    return TRUE;
}
