// Run: node test_ui.js (Node.js is only needed for this development check).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const policy = script.slice(script.indexOf('function dockerTransportAllowed('), script.indexOf('\nfunction disconnectDocker('));
const allowed = vm.runInNewContext(policy + '\ndockerTransportAllowed');
for (const host of ['10.22.29.66', '10.255.255.255', '172.16.0.1', '172.31.255.255',
                    '192.168.0.1', '192.168.255.255', 'localhost', '127.0.0.1', '127.0.0.2', '[::1]']) {
  assert.equal(allowed('http:', host), true, host);
}
for (const host of ['172.15.255.255', '172.32.0.1', '192.169.0.1', '11.0.0.1', '8.8.8.8',
                    'portal.example.com', '10.evil.example', '192.168.1.1.evil.example',
                    '10.999.1.1', '10.1.1', '10..1.1', '[2001:db8::1]']) {
  assert.equal(allowed('http:', host), false, host);
}
assert.equal(allowed('https:', 'portal.example.com'), true);
assert.equal(allowed('file:', 'localhost'), false);
console.log('Docker HTTP address policy and page script syntax checks passed.');
