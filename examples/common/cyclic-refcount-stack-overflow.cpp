/*
 * cyclic-refcount-stack-overflow.cpp
 *
 * Crash type  : SIGSEGV -- stack overflow from unbounded recursive
 *               destruction
 * Mechanism   : A small scene graph holds parent/child nodes as raw owning
 *               pointers with a recursive destructor ("delete every child
 *               on destruction"). `reparent_node()` is supposed to move a
 *               node to a new parent, but forgets to detach it from its
 *               *old* parent's children list first. When the new parent is
 *               a descendant of the node being moved, the node ends up
 *               reachable from two places at once -- silently closing a
 *               cycle in what should be a tree. The program runs fine for
 *               a long time; the crash only happens when the root goes out
 *               of scope and the recursive destructor chases the cycle
 *               forever, blowing the call stack.
 *
 * Complexity  : The symptom is identical to a plain infinite-recursion bug
 *               (SIGSEGV, an enormous backtrace of near-identical repeating
 *               frames) but the defect is NOT in the destructor -- it is in
 *               reparent_node(), called long before the crash and long
 *               since returned. The destructor is correct for a tree; the
 *               bug is that the structure is no longer a tree.
 *
 * What to look for in GDB:
 *   - bt          -- extremely deep, near-identical repeating frames, all
 *                    inside SceneNode::~SceneNode()
 *   - `bt 6`      -- confirms the repeating pattern without dumping
 *                    thousands of frames
 *   - The recursion never reaches a base case ("no children left") --
 *     that is the signal that a cycle exists, not just a deep tree
 *   - Root cause: reparent_node() adds `moving` under `new_parent` without
 *     first removing it from its old parent's children list, so `moving`
 *     becomes reachable from two paths -- one of which loops back
 *
 * Fix hint:
 *   - reparent_node() must remove `moving` from its old parent's children
 *     list, and must reject the move (or explicitly break the cycle) if
 *     `new_parent` is already a descendant of `moving`.
 *   - Prefer std::weak_ptr for the "parent" edge so cycles can't keep nodes
 *     reachable in the first place.
 */
#include <cstdio>
#include <string>
#include <vector>
#include "crashdump.h"

struct SceneNode {
    std::string name;
    SceneNode  *parent = nullptr;
    std::vector<SceneNode *> children;

    explicit SceneNode(std::string n) : name(std::move(n)) {}

    // Recursive destructor -- correct for an actual tree, catastrophic for
    // a graph that (accidentally) contains a cycle.
    ~SceneNode() {
        for (SceneNode *child : children)
            delete child;   // <-- infinite recursion if `children`
                             //     transitively contains `this`
    }

    void add_child(SceneNode *child) {
        child->parent = this;
        children.push_back(child);
    }
};

// BUG: attaches `moving` under `new_parent` but never removes it from its
// *old* parent's children list. If `new_parent` is a descendant of
// `moving`, the graph now contains a cycle that is still reachable from
// the original root through the (never-cleared) old parent link.
static void reparent_node(SceneNode *moving, SceneNode *new_parent) {
    new_parent->add_child(moving);   // no detach from old parent, no cycle check
}

int main() {
    EnableCrashDumps();
    printf("=== Cyclic Reference / Recursive Destructor Stack Overflow Demo ===\n\n");

    // Build: root -> world -> level -> player
    SceneNode *root   = new SceneNode("root");
    SceneNode *world  = new SceneNode("world");
    SceneNode *level  = new SceneNode("level");
    SceneNode *player = new SceneNode("player");

    root->add_child(world);
    world->add_child(level);
    level->add_child(player);

    printf("[main] initial hierarchy: root -> world -> level -> player\n");

    // "Attach the camera rig under the player" -- a reasonable-looking
    // gameplay operation that happens to move an ANCESTOR of `player`
    // (namely `world`) underneath `player` itself.
    printf("[main] reparenting 'world' under 'player' (bug: creates a cycle)...\n");
    reparent_node(world, player);   // world is an ancestor of player -- cycle!

    printf("[main] hierarchy now contains a cycle; tearing down root...\n");
    delete root;   // recursive destructor chases the cycle -- stack overflow

    printf("[main] done\n");
    return 0;
}
