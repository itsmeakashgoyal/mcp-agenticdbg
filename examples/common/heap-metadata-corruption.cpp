/*
 * heap-metadata-corruption.cpp
 *
 * Crash type  : SIGABRT — glibc detects corrupted heap metadata in free()
 *               ("free(): invalid next size" / "malloc(): corrupted top size")
 * Mechanism   : A "packet serialiser" allocates an output buffer that is
 *               exactly the right size for the data — but one of the field
 *               writers (write_string) appends a null terminator without
 *               accounting for it in the size calculation.  The extra byte
 *               overwrites the first byte of the NEXT heap chunk's size
 *               field.  The allocator crash happens dozens of instructions
 *               later, inside a completely unrelated free() call.
 *
 * Complexity  : The corruption site (write_string) and the crash site
 *               (free inside flush_buffer) are in different functions and
 *               separated by several call frames.  Backtrace alone shows
 *               only malloc internals at the top; the root cause requires
 *               looking several frames down and checking the allocation size
 *               arithmetic.
 *
 * What to look for in GDB:
 *   - Top frames: __GI_raise → __GI_abort → malloc_printerr → free internals
 *   - Several frames down: flush_buffer → free(pkt->buf)
 *   - `frame N; print *pkt` shows buf pointer and fields
 *   - `x/32bx pkt->buf` past the end reveals the overwritten chunk header
 *   - Root cause: calc_packet_size() returns N but write_string writes N+1
 *
 * Fix hint:
 *   - calc_packet_size() must add +1 for the null terminator on each string
 *     field, or write_string must not null-terminate (use memcpy, not strcpy).
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>
#include "crashdump.h"

// ---------------------------------------------------------------------------
// Domain: a wire-format packet with typed fields
// ---------------------------------------------------------------------------

enum FieldType : uint8_t { FT_INT32 = 1, FT_FLOAT = 2, FT_STRING = 3 };

struct PacketField {
    FieldType   type;
    const char *name;
    union {
        int32_t  i32;
        float    f32;
        const char *str;
    } value;
};

struct Packet {
    uint8_t  *buf;          // serialised bytes
    size_t    buf_size;     // allocated bytes
    size_t    write_pos;    // current write cursor
    int       field_count;
};

// ---------------------------------------------------------------------------
// Size calculator -- BUG: off-by-one for string null terminator
// ---------------------------------------------------------------------------

static size_t field_wire_size(const PacketField &f) {
    size_t base = 1 + 32;   // type byte + fixed name slot
    switch (f.type) {
        case FT_INT32:  return base + 4;
        case FT_FLOAT:  return base + 4;
        case FT_STRING:
            // BUG: strlen does NOT count the null terminator
            // but write_string below uses strcpy which writes strlen+1 bytes
            return base + strlen(f.value.str);  // should be +1
    }
    return base;
}

static size_t calc_packet_size(const PacketField *fields, int n) {
    size_t total = 8;   // 4-byte magic + 4-byte field count
    for (int i = 0; i < n; i++)
        total += field_wire_size(fields[i]);
    return total;
}

// ---------------------------------------------------------------------------
// Serialiser
// ---------------------------------------------------------------------------

static void write_u8(Packet *p, uint8_t v) {
    p->buf[p->write_pos++] = v;
}
static void write_u32(Packet *p, uint32_t v) {
    memcpy(p->buf + p->write_pos, &v, 4);
    p->write_pos += 4;
}
static void write_fixed_name(Packet *p, const char *name) {
    memset(p->buf + p->write_pos, 0, 32);
    strncpy((char*)p->buf + p->write_pos, name, 31);
    p->write_pos += 32;
}
static void write_string(Packet *p, const char *s) {
    // BUG: strcpy writes strlen(s)+1 bytes (including '\0')
    // but the buffer was only allocated for strlen(s) bytes.
    // The extra '\0' overwrites the first byte of the next heap chunk header.
    strcpy((char*)p->buf + p->write_pos, s);    // use memcpy+len to fix
    p->write_pos += strlen(s);                  // cursor advances by strlen only
}

static void serialize_field(Packet *p, const PacketField &f) {
    write_u8(p, (uint8_t)f.type);
    write_fixed_name(p, f.name);
    switch (f.type) {
        case FT_INT32:  write_u32(p, (uint32_t)f.value.i32);  break;
        case FT_FLOAT:  write_u32(p, *(uint32_t*)&f.value.f32); break;
        case FT_STRING: write_string(p, f.value.str);          break;
    }
}

static Packet *build_packet(const PacketField *fields, int n) {
    Packet *p = (Packet*)malloc(sizeof(Packet));
    p->buf_size  = calc_packet_size(fields, n);     // undersized for strings
    p->buf       = (uint8_t*)malloc(p->buf_size);   // heap chunk allocated here
    p->write_pos = 0;
    p->field_count = n;

    write_u32(p, 0xC0DE1234);   // magic
    write_u32(p, (uint32_t)n);  // field count

    for (int i = 0; i < n; i++)
        serialize_field(p, fields[i]);  // overflow happens inside here

    return p;
}

// ---------------------------------------------------------------------------
// "Transport" layer -- crash happens here, far from the overflow site
// ---------------------------------------------------------------------------

struct Transport {
    char endpoint[128];
    int  bytes_sent;
};

static Transport *g_transport = nullptr;

static void flush_buffer(Packet *p) {
    // Simulate sending: just print and free
    printf("[transport] flushing %zu bytes to %s\n",
           p->write_pos, g_transport->endpoint);
    g_transport->bytes_sent += (int)p->write_pos;

    free(p->buf);       // <-- SIGABRT: malloc detects the corrupted chunk header
    free(p);
}

static void send_telemetry(const char *device_id, int temp, float voltage,
                           const char *firmware_ver) {
    PacketField fields[] = {
        { FT_STRING, "device_id",    { .str = device_id    } },
        { FT_INT32,  "temperature",  { .i32 = temp         } },
        { FT_FLOAT,  "voltage",      { .f32 = voltage      } },
        { FT_STRING, "firmware_ver", { .str = firmware_ver } },
    };
    int n = sizeof(fields) / sizeof(fields[0]);

    Packet *p = build_packet(fields, n);
    flush_buffer(p);    // crash here — heap metadata corrupted during build
}

// ---------------------------------------------------------------------------

int main(void) {
    EnableCrashDumps();
    printf("=== Heap Metadata Corruption Demo ===\n\n");

    g_transport = (Transport*)calloc(1, sizeof(Transport));
    strncpy(g_transport->endpoint, "telemetry.iot.local:9000",
            sizeof(g_transport->endpoint) - 1);

    // First packet: short strings — fits OK (overflow is tiny, may not corrupt)
    printf("[main] sending telemetry batch 1...\n");
    send_telemetry("dev-001", 42, 3.3f, "v1.2");

    // Second packet: strings exactly filling their wire slots — off-by-one fires,
    // corrupting the heap chunk that follows the buffer allocation.
    printf("[main] sending telemetry batch 2...\n");
    // firmware_ver length chosen to guarantee the null byte lands on the next chunk
    send_telemetry("dev-sensor-kitchen-002", 37, 3.28f, "v2.14.0-release-build");

    printf("[main] done — bytes_sent=%d\n", g_transport->bytes_sent);
    free(g_transport);
    return 0;
}
