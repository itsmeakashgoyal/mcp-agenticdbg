/*
 * deep-callchain-nullptr.cpp
 *
 * Crash type  : SIGSEGV — null pointer dereference 12+ frames deep
 * Mechanism   : A small expression evaluator walks an AST built from a
 *               config string.  One node type ("Call") stores a pointer to
 *               a resolved function descriptor.  When the resolver cannot
 *               find the function name it returns nullptr instead of
 *               throwing, and the evaluator happily stores the null.
 *               Evaluation later dereferences the null descriptor pointer
 *               ~12 call frames down from main().
 *
 * Complexity  : Deep mutual recursion; the faulting frame is inside
 *               evaluate_call(), but the bug is in resolve_function() which
 *               ran earlier during parse().  Inspecting locals at frames
 *               3–8 is needed to trace the nullptr back to its origin.
 *
 * What to look for in GDB:
 *   - bt full  -- shows 12+ frames; crash in evaluate_call at the deref
 *   - frame 2..5; print *node  -- reveals fn_desc == 0x0
 *   - frame 8..10              -- shows resolve_function returning nullptr
 *   - Root cause: resolve_function() should error on unknown names, not
 *                 silently return nullptr
 *
 * Fix hint:
 *   - In resolve_function(), throw or return an error node when the name
 *     is not found instead of returning nullptr.
 *   - In CallNode constructor, assert(fn_desc != nullptr).
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdexcept>
#include <vector>
#include <string>
#include "crashdump.h"

// ---------------------------------------------------------------------------
// AST node types
// ---------------------------------------------------------------------------

enum NodeKind { NK_LIT, NK_VAR, NK_BINOP, NK_UNOP, NK_CALL, NK_BLOCK };

struct FuncDescriptor {
    const char *name;
    int         arity;
    double    (*impl)(const double *args, int n);
};

struct AstNode {
    NodeKind kind;

    // NK_LIT
    double literal;

    // NK_VAR
    char var_name[32];

    // NK_BINOP / NK_UNOP
    char op;
    AstNode *left;
    AstNode *right;      // nullptr for unary

    // NK_CALL
    FuncDescriptor *fn_desc;    // BUG: can be nullptr when name not found
    std::vector<AstNode *> args;

    // NK_BLOCK
    std::vector<AstNode *> stmts;
    AstNode *result_expr;

    AstNode() : kind(NK_LIT), literal(0), op(0),
                left(nullptr), right(nullptr),
                fn_desc(nullptr), result_expr(nullptr) {
        memset(var_name, 0, sizeof(var_name));
    }
};

// ---------------------------------------------------------------------------
// "Standard library" of built-in functions
// ---------------------------------------------------------------------------

static double builtin_add(const double *a, int n)  { return n >= 2 ? a[0]+a[1] : 0; }
static double builtin_mul(const double *a, int n)  { return n >= 2 ? a[0]*a[1] : 0; }
static double builtin_neg(const double *a, int n)  { return n >= 1 ? -a[0] : 0; }
static double builtin_abs(const double *a, int n)  { return n >= 1 ? (a[0]<0?-a[0]:a[0]) : 0; }
static double builtin_max(const double *a, int n)  { return n >= 2 ? (a[0]>a[1]?a[0]:a[1]) : 0; }

static FuncDescriptor g_builtins[] = {
    {"add", 2, builtin_add},
    {"mul", 2, builtin_mul},
    {"neg", 1, builtin_neg},
    {"abs", 1, builtin_abs},
    {"max", 2, builtin_max},
    // Note: "transform" is intentionally missing — triggers the bug
    {nullptr, 0, nullptr}
};

// ---------------------------------------------------------------------------
// Resolver (bug lives here)
// ---------------------------------------------------------------------------

static FuncDescriptor *resolve_function(const char *name) {
    for (int i = 0; g_builtins[i].name; i++) {
        if (strcmp(g_builtins[i].name, name) == 0)
            return &g_builtins[i];
    }
    // BUG: should throw std::runtime_error("unknown function: " + name)
    // Instead silently returns nullptr, propagating the null into the AST
    fprintf(stderr, "[resolver] WARNING: unknown function '%s' — returning nullptr\n", name);
    return nullptr;
}

// ---------------------------------------------------------------------------
// Parser (simplified hand-written)
// ---------------------------------------------------------------------------

static AstNode *make_lit(double v) {
    AstNode *n = new AstNode(); n->kind = NK_LIT; n->literal = v; return n;
}
static AstNode *make_binop(char op, AstNode *l, AstNode *r) {
    AstNode *n = new AstNode(); n->kind = NK_BINOP; n->op = op;
    n->left = l; n->right = r; return n;
}
static AstNode *make_call(const char *name, std::vector<AstNode*> args) {
    AstNode *n = new AstNode(); n->kind = NK_CALL;
    n->fn_desc = resolve_function(name);   // may be nullptr
    n->args = std::move(args);
    return n;
}
static AstNode *make_block(std::vector<AstNode*> stmts, AstNode *result) {
    AstNode *n = new AstNode(); n->kind = NK_BLOCK;
    n->stmts = std::move(stmts); n->result_expr = result; return n;
}

// Builds an AST that represents:
//   block {
//     t1 = add(3, mul(2, abs(-5)))
//     t2 = transform(t1, max(t1, 10))   <-- "transform" unknown → nullptr fn_desc
//     result = add(t1, t2)
//   }
static AstNode *build_program() {
    // add(3, mul(2, abs(-5)))  =>  add(3, mul(2, 5))  => add(3, 10) => 13
    AstNode *inner_abs = make_call("abs",  { make_lit(-5) });
    AstNode *inner_mul = make_call("mul",  { make_lit(2), inner_abs });
    AstNode *t1        = make_call("add",  { make_lit(3), inner_mul });

    // max(t1, 10)
    AstNode *t1b = make_call("add", { make_lit(3), make_lit(10) }); // recompute t1
    AstNode *mx  = make_call("max", { t1b, make_lit(10) });

    // transform(t1, max(t1,10))  -- UNKNOWN FUNCTION -> fn_desc == nullptr
    AstNode *t2 = make_call("transform", { make_call("add",{make_lit(3),make_lit(10)}), mx });

    // add(t1, t2)
    AstNode *result = make_call("add", { make_call("add",{make_lit(3),make_lit(10)}), t2 });

    return make_block({ t1, t2 }, result);
}

// ---------------------------------------------------------------------------
// Evaluator (recursive, crash 12+ frames deep)
// ---------------------------------------------------------------------------

struct EvalContext {
    double vars[16];
    char   var_names[16][32];
    int    var_count;
    int    depth;

    double get(const char *name) const {
        for (int i = 0; i < var_count; i++)
            if (strcmp(var_names[i], name) == 0) return vars[i];
        return 0.0;
    }
    void set(const char *name, double val) {
        for (int i = 0; i < var_count; i++)
            if (strcmp(var_names[i], name) == 0) { vars[i] = val; return; }
        if (var_count < 16) {
            strncpy(var_names[var_count], name, 31);
            vars[var_count++] = val;
        }
    }
};

static double evaluate(const AstNode *node, EvalContext &ctx);

static double evaluate_call(const AstNode *node, EvalContext &ctx) {
    ctx.depth++;
    // Evaluate all argument expressions first
    std::vector<double> arg_vals;
    for (AstNode *a : node->args)
        arg_vals.push_back(evaluate(a, ctx));

    // BUG: node->fn_desc is nullptr when the function was not resolved
    // This crashes on the next line when we dereference fn_desc->arity
    if ((int)arg_vals.size() != node->fn_desc->arity) {    // <-- SIGSEGV here
        fprintf(stderr, "arity mismatch\n");
        return 0;
    }
    double result = node->fn_desc->impl(arg_vals.data(), (int)arg_vals.size());
    ctx.depth--;
    return result;
}

static double evaluate_binop(const AstNode *node, EvalContext &ctx) {
    double l = evaluate(node->left,  ctx);
    double r = evaluate(node->right, ctx);
    switch (node->op) {
        case '+': return l + r;
        case '-': return l - r;
        case '*': return l * r;
        case '/': return r != 0 ? l / r : 0;
    }
    return 0;
}

static double evaluate_block(const AstNode *node, EvalContext &ctx) {
    for (AstNode *stmt : node->stmts)
        evaluate(stmt, ctx);
    return node->result_expr ? evaluate(node->result_expr, ctx) : 0.0;
}

static double evaluate(const AstNode *node, EvalContext &ctx) {
    if (!node) return 0.0;
    switch (node->kind) {
        case NK_LIT:   return node->literal;
        case NK_VAR:   return ctx.get(node->var_name);
        case NK_BINOP: return evaluate_binop(node, ctx);
        case NK_UNOP:  return evaluate(node->left, ctx);
        case NK_CALL:  return evaluate_call(node, ctx);    // recurses here
        case NK_BLOCK: return evaluate_block(node, ctx);
    }
    return 0.0;
}

// ---------------------------------------------------------------------------

static double run_program(AstNode *prog) {
    EvalContext ctx{};
    ctx.depth = 0;
    return evaluate(prog, ctx);
}

int main(void) {
    EnableCrashDumps();
    printf("=== Deep Call-Chain Null Dereference Demo ===\n\n");

    printf("[main] building AST...\n");
    AstNode *prog = build_program();

    printf("[main] evaluating program...\n");
    double result = run_program(prog);   // crash inside evaluate_call ~12 frames deep

    printf("[main] result = %.4f\n", result);
    return 0;
}
