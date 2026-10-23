'use strict';

const fill = require('fill-range');
const stringify = require('./stringify');
const utils = require('./utils');
const { MAX_DEPTH } = require('./constants');

const append = (queue = '', stash = '', enclose = false) => {
  const result = [];

  queue = [].concat(queue);
  stash = [].concat(stash);

  if (!stash.length) return queue;
  if (!queue.length) {
    return enclose ? utils.flatten(stash).map(ele => `{${ele}}`) : stash;
  }

  for (const item of queue) {
    if (Array.isArray(item)) {
      for (const value of item) {
        result.push(append(value, stash, enclose));
      }
    } else {
      for (let ele of stash) {
        if (enclose === true && typeof ele === 'string') ele = `{${ele}}`;
        result.push(Array.isArray(ele) ? append(item, ele, enclose) : item + ele);
      }
    }
  }
  return utils.flatten(result);
};

const queueOwner = node => {
  if (node.type === 'brace' || node.type === 'root' || !node.parent) return node;

  const seen = new Set();
  while (node.type !== 'brace' && node.type !== 'root' && node.parent) {
    if (seen.has(node)) {
      throw new RangeError('AST parent chain contains a cycle');
    }
    seen.add(node);
    node = node.parent;
  }
  return node;
};

const expand = (ast, options = {}) => {
  const rangeLimit = options.rangeLimit === undefined ? 1000 : options.rangeLimit;
  const maxDepth = Number.isFinite(options.maxDepth) ? Math.min(MAX_DEPTH, options.maxDepth) : MAX_DEPTH;

  const walk = (node, parent = {}, depth = 0) => {
    if (node.nodes && depth > maxDepth) {
      throw new RangeError(`AST depth (${depth}), exceeds max depth (${maxDepth})`);
    }
    node.queue = [];

    const q = queueOwner(parent).queue;

    if (node.invalid || node.dollar) {
      q.push(append(q.pop(), stringify(node, options)));
      return;
    }

    if (node.type === 'brace' && node.invalid !== true && node.nodes.length === 2) {
      q.push(append(q.pop(), ['{}']));
      return;
    }

    if (node.nodes && node.ranges > 0) {
      const args = utils.reduce(node.nodes);

      if (utils.exceedsLimit(...args, options.step, rangeLimit)) {
        throw new RangeError('expanded array length exceeds range limit. Use options.rangeLimit to increase or disable the limit.');
      }

      let range = fill(...args, options);
      if (range.length === 0) {
        range = stringify(node, options);
      }

      q.push(append(q.pop(), range));
      node.nodes = [];
      return;
    }

    const enclose = utils.encloseBrace(node);
    const queue = queueOwner(node).queue;

    for (let i = 0; i < node.nodes.length; i++) {
      const child = node.nodes[i];

      if (child.type === 'comma' && node.type === 'brace') {
        if (i === 1) queue.push('');
        queue.push('');
        continue;
      }

      if (child.type === 'close') {
        q.push(append(q.pop(), queue, enclose));
        continue;
      }

      if (child.value && child.type !== 'open') {
        queue.push(append(queue.pop(), child.value));
        continue;
      }

      if (child.nodes) {
        walk(child, node, child.nodes ? depth + 1 : depth);
      }
    }

    return queue;
  };

  return utils.flatten(walk(ast, {}, ast.type === 'root' ? 0 : 1));
};

module.exports = expand;
