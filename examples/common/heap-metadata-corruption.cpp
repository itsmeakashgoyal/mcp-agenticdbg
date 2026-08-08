/*
 * heap-metadata-corruption.cpp
 *
 * Crash type  : SIGABRT — glibc detects corrupted heap metadata in free()
 *               ("malloc(): invalid size" / "munmap_chunk(): invalid
 *               pointer" / similar, depending on glibc version)
 * Mechanism   : A "packet serialiser" allocates an output buffer sized by
 *               calc_packet_size() — but that calculator has a real
 *               off-by-one bug: field_wire_size() forgets to budget for the
 *               null terminator write_string() appends via strcpy(). Every
 *               string field therefore overflows the buffer by one byte,
 *               corrupting whatever heap chunk immediately follows it. That
 *               chunk (a small per-connection tracking record allocated
 *               right after the buffer) isn't touched again until transport
 *               cleanup runs — so the allocator crash happens later, inside
 *               a completely unrelated free() call in flush_buffer(), far
 *               from where the actual overflow occurred.
 *
 * Complexity  : The corruption site (write_string) and the crash site
 *               (free inside flush_buffer) are in different functions and
 *               separated by several call frames. Backtrace alone shows
 *               only malloc internals at the top; the root cause requires
 *               looking several frames down and checking the allocation
 *               size arithmetic.
 *
 * Reliability note: whether a 1-byte overflow actually corrupts something
 * the allocator validates depends on exactly which byte of a neighboring
 * chunk's header it lands on and on internal chunk-size rounding -- not
 * something this demo can pin down byte-for-byte across glibc versions.
 * To make it reproduce deterministically, build_packet() allocates that
 * tracking record immediately after the packet buffer (adjacent, since
 * nothing has been freed yet on this heap) and, right where the real
 * accounting bug's overflow lands, stomps the record's *own* chunk header
 * with a value no allocator would ever accept as a valid chunk size --
 * freeing the buffer itself still succeeds normally; it's the later free()
 * of the tracking record, whose header we corrupted, that reliably aborts.
 *
 * What to look for in GDB:
 *   - Top frames: __GI_raise → __GI_abort → malloc_printerr → free internals
 *   - Several frames down: flush_buffer → free(pkt->conn_record)
 *   - `frame N; print *pkt` shows buf pointer and fields
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

#if defined(__linux__) && defined(__GLIBC__)
#include <malloc.h>
#define HMC_GLIBC_TECHNIQUE 1
#endif

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
    void     *conn_record;  // per-connection tracking record (see build_packet)
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
    // The extra '\0' overwrites the first byte past the buffer.
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
    p->conn_record = nullptr;

#ifdef HMC_GLIBC_TECHNIQUE
    // A small per-connection tracking record, allocated immediately after
    // `buf` on this still-fresh heap -- adjacent in memory, exactly where
    // the accounting bug above overflows into.
    p->conn_record = malloc(64);
#endif

    p->write_pos = 0;
    p->field_count = n;

    write_u32(p, 0xC0DE1234);   // magic
    write_u32(p, (uint32_t)n);  // field count

    for (int i = 0; i < n; i++)
        serialize_field(p, fields[i]);  // overflow happens inside here

#ifdef HMC_GLIBC_TECHNIQUE
    // The off-by-one above is a real bug, but its exact effect depends on
    // string lengths and glibc's internal chunk-size rounding -- not
    // something a demo can pin down byte-for-byte across allocator
    // versions. To make the corruption land deterministically on the
    // tracking record's own header (the same offset any glibc version uses
    // for "the chunk immediately after buf"), stomp it explicitly with an
    // unmistakably invalid size, demonstrating what the accounting bug
    // causes without depending on exact byte-level luck. Freeing `buf`
    // itself is unaffected -- only the tracking record's own header is
    // corrupted, so *its* eventual free() is what aborts.
    size_t *conn_record_size_field = (size_t *)(p->buf + malloc_usable_size(p->buf));
    *conn_record_size_field = (size_t)0xDEADBEEFDEADBEEFULL;
#endif

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

    free(p->buf);            // succeeds -- buf's own header was never touched
    free(p->conn_record);    // <-- SIGABRT: this record's header was stomped
    free(p);
}

// Designated initializers (`{ .str = ... }`) are a C++20 feature that GCC
// and Clang also tolerate as an extension under -std=c++17, but MSVC does
// not without /std:c++20 -- so PacketField values are built through these
// helpers instead, which are portable to every compiler/standard in use
// across examples/linux, examples/macos, and examples/windows.
static PacketField string_field(const char *name, const char *value) {
    PacketField f;
    f.type = FT_STRING;
    f.name = name;
    f.value.str = value;
    return f;
}
static PacketField int_field(const char *name, int32_t value) {
    PacketField f;
    f.type = FT_INT32;
    f.name = name;
    f.value.i32 = value;
    return f;
}
static PacketField float_field(const char *name, float value) {
    PacketField f;
    f.type = FT_FLOAT;
    f.name = name;
    f.value.f32 = value;
    return f;
}

static void send_telemetry(const char *device_id, int temp, float voltage,
                           const char *firmware_ver) {
    PacketField fields[] = {
        string_field("device_id", device_id),
        int_field("temperature", temp),
        float_field("voltage", voltage),
        string_field("firmware_ver", firmware_ver),
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

    printf("[main] sending telemetry batch...\n");
    send_telemetry("dev-sensor-kitchen-002", 37, 3.28f, "v2.14.0-release-build");

    printf("[main] done — bytes_sent=%d\n", g_transport->bytes_sent);
    free(g_transport);
    return 0;
}
