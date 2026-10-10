/*
 * The part of the TWAIN 2.4 specification (twain.h) that the virtual scanner
 * needs. Values and layouts follow the specification exactly; Windows packs
 * TWAIN structures on 2-byte boundaries, and the size checks at the bottom
 * stop the build if that ever drifts.
 */
#pragma once
#include <windows.h>
#include <stdint.h>

#define TWRC_SUCCESS 0
#define TWRC_FAILURE 1
#define TWRC_CHECKSTATUS 2
#define TWRC_CANCEL 3
#define TWRC_DSEVENT 4
#define TWRC_NOTDSEVENT 5
#define TWRC_XFERDONE 6
#define TWRC_ENDOFLIST 7

#define TWCC_SUCCESS 0
#define TWCC_BUMMER 1
#define TWCC_LOWMEMORY 2
#define TWCC_MAXCONNECTIONS 4
#define TWCC_OPERATIONERROR 5
#define TWCC_BADCAP 6
#define TWCC_BADPROTOCOL 9
#define TWCC_BADVALUE 10
#define TWCC_SEQERROR 11
#define TWCC_CAPUNSUPPORTED 13
#define TWCC_CAPBADOPERATION 14
#define TWCC_PAPERJAM 20
#define TWCC_CHECKDEVICEONLINE 23
#define TWCC_NOMEDIA 29

#define DG_CONTROL 0x0001UL
#define DG_IMAGE 0x0002UL
#define DF_DSM2 0x10000000UL
#define DF_APP2 0x20000000UL
#define DF_DS2 0x40000000UL

#define DAT_NULL 0x0000
#define DAT_CAPABILITY 0x0001
#define DAT_EVENT 0x0002
#define DAT_IDENTITY 0x0003
#define DAT_PARENT 0x0004
#define DAT_PENDINGXFERS 0x0005
#define DAT_SETUPMEMXFER 0x0006
#define DAT_STATUS 0x0008
#define DAT_USERINTERFACE 0x0009
#define DAT_XFERGROUP 0x000a
#define DAT_IMAGEINFO 0x0101
#define DAT_IMAGELAYOUT 0x0102
#define DAT_IMAGEMEMXFER 0x0103
#define DAT_IMAGENATIVEXFER 0x0104
#define DAT_ENTRYPOINT 0x0403

#define MSG_NULL 0x0000
#define MSG_GET 0x0001
#define MSG_GETCURRENT 0x0002
#define MSG_GETDEFAULT 0x0003
#define MSG_SET 0x0006
#define MSG_RESET 0x0007
#define MSG_QUERYSUPPORT 0x0008
#define MSG_XFERREADY 0x0101
#define MSG_CLOSEDSREQ 0x0102
#define MSG_OPENDS 0x0401
#define MSG_CLOSEDS 0x0402
#define MSG_DISABLEDS 0x0501
#define MSG_ENABLEDS 0x0502
#define MSG_ENABLEDSUIONLY 0x0503
#define MSG_PROCESSEVENT 0x0601
#define MSG_ENDXFER 0x0701
#define MSG_RESETALL 0x0A01

#define TWON_ARRAY 3
#define TWON_ENUMERATION 4
#define TWON_ONEVALUE 5
#define TWON_RANGE 6

#define TWTY_INT8 0x0000
#define TWTY_INT16 0x0001
#define TWTY_INT32 0x0002
#define TWTY_UINT8 0x0003
#define TWTY_UINT16 0x0004
#define TWTY_UINT32 0x0005
#define TWTY_BOOL 0x0006
#define TWTY_FIX32 0x0007

#define TWQC_GET 0x0001
#define TWQC_SET 0x0002
#define TWQC_GETDEFAULT 0x0004
#define TWQC_GETCURRENT 0x0008
#define TWQC_RESET 0x0010

#define CAP_XFERCOUNT 0x0001
#define ICAP_COMPRESSION 0x0100
#define ICAP_PIXELTYPE 0x0101
#define ICAP_UNITS 0x0102
#define ICAP_XFERMECH 0x0103
#define CAP_FEEDERENABLED 0x1002
#define CAP_FEEDERLOADED 0x1003
#define CAP_SUPPORTEDCAPS 0x1005
#define CAP_AUTOFEED 0x1007
#define CAP_INDICATORS 0x100b
#define CAP_PAPERDETECTABLE 0x100d
#define CAP_UICONTROLLABLE 0x100e
#define CAP_DEVICEONLINE 0x100f
#define CAP_DUPLEX 0x1012
#define CAP_DUPLEXENABLED 0x1013
#define ICAP_PHYSICALWIDTH 0x1111
#define ICAP_PHYSICALHEIGHT 0x1112
#define ICAP_XNATIVERESOLUTION 0x1116
#define ICAP_YNATIVERESOLUTION 0x1117
#define ICAP_XRESOLUTION 0x1118
#define ICAP_YRESOLUTION 0x1119
#define ICAP_PIXELFLAVOR 0x111f
#define ICAP_SUPPORTEDSIZES 0x1122
#define ICAP_BITDEPTH 0x112b

#define TWPT_BW 0
#define TWPT_GRAY 1
#define TWPT_RGB 2
#define TWUN_INCHES 0
#define TWSX_NATIVE 0
#define TWSX_MEMORY 2
#define TWCP_NONE 0
#define TWPF_CHOCOLATE 0
#define TWDX_1PASSDUPLEX 1
#define TWSS_NONE 0
#define TWSS_A4 1
#define TWSS_USLETTER 3
#define TWSS_USLEGAL 4
#define TWMF_APPOWNS 0x0001
#define TWMF_POINTER 0x0008

#pragma pack(push, 2)
typedef struct { int16_t Whole; uint16_t Frac; } TW_FIX32;
typedef struct { TW_FIX32 Left, Top, Right, Bottom; } TW_FRAME;
typedef char TW_STR32[34];
typedef struct {
    uint16_t MajorNum, MinorNum, Language, Country;
    TW_STR32 Info;
} TW_VERSION;
typedef struct {
    uint32_t Id;
    TW_VERSION Version;
    uint16_t ProtocolMajor, ProtocolMinor;
    uint32_t SupportedGroups;
    TW_STR32 Manufacturer, ProductFamily, ProductName;
} TW_IDENTITY;
typedef struct { uint16_t ShowUI, ModalUI; HANDLE hParent; } TW_USERINTERFACE;
typedef struct { uint16_t Cap, ConType; HANDLE hContainer; } TW_CAPABILITY;
typedef struct { uint16_t ItemType; uint32_t Item; } TW_ONEVALUE;
typedef struct {
    uint16_t ItemType;
    uint32_t NumItems, CurrentIndex, DefaultIndex;
    uint8_t ItemList[1];
} TW_ENUMERATION;
typedef struct { uint16_t ItemType; uint32_t NumItems; uint8_t ItemList[1]; } TW_ARRAY;
typedef struct { void *pEvent; uint16_t TWMessage; } TW_EVENT;
typedef struct { uint16_t Count; uint32_t EOJ; } TW_PENDINGXFERS;
typedef struct { uint16_t ConditionCode; uint16_t Data; } TW_STATUS;
typedef struct {
    TW_FIX32 XResolution, YResolution;
    int32_t ImageWidth, ImageLength;
    int16_t SamplesPerPixel;
    int16_t BitsPerSample[8];
    int16_t BitsPerPixel;
    uint16_t Planar;
    int16_t PixelType;
    uint16_t Compression;
} TW_IMAGEINFO;
typedef struct { TW_FRAME Frame; uint32_t DocumentNumber, PageNumber, FrameNumber; } TW_IMAGELAYOUT;
typedef struct { uint32_t MinBufSize, MaxBufSize, Preferred; } TW_SETUPMEMXFER;
typedef struct { uint32_t Flags, Length; void *TheMem; } TW_MEMORY;
typedef struct {
    uint16_t Compression;
    uint32_t BytesPerRow, Columns, Rows, XOffset, YOffset, BytesWritten;
    TW_MEMORY Memory;
} TW_IMAGEMEMXFER;
typedef uint16_t (WINAPI *DSMENTRYPROC)(TW_IDENTITY *, TW_IDENTITY *, uint32_t, uint16_t, uint16_t, void *);
typedef HANDLE (WINAPI *DSM_MEMALLOCATE)(uint32_t);
typedef void (WINAPI *DSM_MEMFREE)(HANDLE);
typedef void *(WINAPI *DSM_MEMLOCK)(HANDLE);
typedef void (WINAPI *DSM_MEMUNLOCK)(HANDLE);
typedef struct {
    uint32_t Size;
    DSMENTRYPROC DSM_Entry;
    DSM_MEMALLOCATE DSM_MemAllocate;
    DSM_MEMFREE DSM_MemFree;
    DSM_MEMLOCK DSM_MemLock;
    DSM_MEMUNLOCK DSM_MemUnlock;
} TW_ENTRYPOINT;
#pragma pack(pop)

#define TW_PTR ((int)sizeof(void *))
static_assert(sizeof(TW_FIX32) == 4, "TW_FIX32");
static_assert(sizeof(TW_IDENTITY) == 156, "TW_IDENTITY");
static_assert(sizeof(TW_ONEVALUE) == 6, "TW_ONEVALUE");
static_assert(sizeof(TW_PENDINGXFERS) == 6, "TW_PENDINGXFERS");
static_assert(sizeof(TW_STATUS) == 4, "TW_STATUS");
static_assert(sizeof(TW_IMAGEINFO) == 42, "TW_IMAGEINFO");
static_assert(sizeof(TW_IMAGELAYOUT) == 28, "TW_IMAGELAYOUT");
static_assert(sizeof(TW_SETUPMEMXFER) == 12, "TW_SETUPMEMXFER");
static_assert(sizeof(TW_USERINTERFACE) == 4 + TW_PTR, "TW_USERINTERFACE");
static_assert(sizeof(TW_CAPABILITY) == 4 + TW_PTR, "TW_CAPABILITY");
static_assert(sizeof(TW_EVENT) == TW_PTR + 2, "TW_EVENT");
static_assert(sizeof(TW_IMAGEMEMXFER) == 34 + TW_PTR, "TW_IMAGEMEMXFER");
static_assert(sizeof(TW_ENTRYPOINT) == 4 + 5 * TW_PTR, "TW_ENTRYPOINT");
