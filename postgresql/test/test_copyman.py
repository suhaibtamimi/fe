##
# .test.test_copyman - test .copyman
##
from sys import stdout
import unittest
from itertools import islice
from .. import copyman
from ..temporal import pg_tmp
# The asyncs, and alternative termination.
from ..protocol.element3 import Notice, Notify, Error, cat_messages

# state manager can handle empty data messages, right? =)
emptysource = """
CREATE TEMP TABLE emptysource ();
-- 10
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
INSERT INTO emptysource DEFAULT VALUES;
"""
emptydst = "CREATE TEMP TABLE empty ();"

# The usual subjects.
stdrowcount = 10000
stdsource = """
CREATE TEMP TABLE source (i int, t text);
INSERT INTO source
	SELECT i, i::text AS t
	FROM generate_series(1, {0}) AS g(i);
""".format(stdrowcount)
stditer = [
	b'\t'.join((x, x)) + b'\n'
	for x in (
		str(i).encode('ascii') for i in range(1, 10001)
	)
]
stditer_tuples = [
	(x, str(x)) for x in range(1, 10001)
]

stddst = "CREATE TEMP TABLE destination (i int, t text)"
srcsql = "COPY source TO STDOUT"
dstsql = "COPY destination FROM STDIN"
binary_srcsql = "COPY source TO STDOUT WITH BINARY"
binary_dstsql = "COPY destination FROM STDIN WITH BINARY"
dstcount = "SELECT COUNT(*) FROM destination"
grabdst = "SELECT * FROM destination ORDER BY i ASC"
grabsrc = "SELECT * FROM source ORDER BY i ASC"

##
# This subclass is used to append some arbitrary data
# after the initial data. This is used to exercise async/notice support.
class Injector(copyman.StatementProducer):
	def __init__(self, appended_messages, *args, **kw):
		super().__init__(*args, **kw)
		self._appended_messages = appended_messages

	def confiscate(self):
		pq = self.statement.database.pq
		mb = pq.message_buffer
		b = mb.getvalue()
		mb.truncate()
		mb.write(cat_messages(self._appended_messages))
		mb.write(b)
		return super().confiscate()

class test_copyman(unittest.TestCase):
	def testNull(self):
		# Test some of the basic machinery.
		sp = copyman.NullProducer()
		sr = copyman.NullReceiver()
		copyman.CopyManager(sp, sr).run()
		self.failUnlessEqual(sp.total_messages, 0)
		self.failUnlessEqual(sp.total_bytes, 0)

	@pg_tmp
	def testNullProducer(self):
		sqlexec(stddst)
		np = copyman.NullProducer()
		sr = copyman.StatementReceiver(prepare(dstsql))
		copyman.CopyManager(np, sr).run()
		self.failUnlessEqual(np.total_messages, 0)
		self.failUnlessEqual(np.total_bytes, 0)
		self.failUnlessEqual(prepare(dstcount).first(), 0)
		self.failUnlessEqual(prepare(grabdst)(), [])

	@pg_tmp
	def testNullReceiver(self):
		sqlexec(stdsource)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 128)
		sr = copyman.NullReceiver()
		with copyman.CopyManager(sp, sr) as copy:
			for x in copy:
				pass
		self.failUnlessEqual(sp.total_messages, stdrowcount)
		self.failUnlessEqual(sp.total_bytes > 0, True)

	def testIteratorToCall(self):
		tmp = iter(stditer)
		# segment stditer into chunks consisting of twenty rows each
		sp = copyman.IteratorProducer([
			list(islice(tmp, 20)) for x in range(len(stditer) // 20)
		])
		dest = []
		sr = copyman.CallReceiver(dest.extend)
		recomputed_bytes = 0
		recomputed_messages = 0
		with copyman.CopyManager(sp, sr) as copy:
			for msg, bytes in copy:
				recomputed_messages += msg
				recomputed_bytes += bytes
		self.failUnlessEqual(stdrowcount, recomputed_messages)
		self.failUnlessEqual(recomputed_bytes, sp.total_bytes)
		self.failUnlessEqual(len(dest), stdrowcount)
		self.failUnlessEqual(dest, stditer)

	@pg_tmp
	def testDirectStatements(self):
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 512)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))
		with copyman.CopyManager(sp, sr) as copy:
			for x in copy:
				pass
		self.failUnlessEqual(dst.prepare(dstcount).first(), stdrowcount)
		self.failUnlessEqual(dst.prepare(grabdst)(), prepare(grabsrc)())

	@pg_tmp
	def testIteratorProducer(self):
		sqlexec(stddst)
		sp = copyman.IteratorProducer([stditer])
		sr = copyman.StatementReceiver(prepare(dstsql))
		recomputed_bytes = 0
		recomputed_messages = 0
		with copyman.CopyManager(sp, sr) as copy:
			for msg, bytes in copy:
				recomputed_messages += msg
				recomputed_bytes += bytes
		self.failUnlessEqual(stdrowcount, recomputed_messages)
		self.failUnlessEqual(recomputed_bytes, sp.total_bytes)
		self.failUnlessEqual(prepare(dstcount).first(), stdrowcount)
		self.failUnlessEqual(prepare(grabdst)(), stditer_tuples)

	def multiple_destinations(self, count = 3, binary = False, buffer_size = 129):
		if binary:
			src = binary_srcsql
			dst = binary_dstsql
			# accommodate for the binary header.
			count_offset = 1
		else:
			src = srcsql
			dst = dstsql
			count_offset = 0
		sqlexec(stdsource)
		dests = [new() for x in range(count)]
		receivers = []
		for x in dests:
			x.execute(stddst)
			receivers.append(copyman.StatementReceiver(x.prepare(dst)))
		sp = copyman.StatementProducer(prepare(src), buffer_size = buffer_size)
		recomputed_bytes = 0
		recomputed_messages = 0
		with copyman.CopyManager(sp, *receivers) as copy:
			for msg, bytes in copy:
				recomputed_messages += msg
				recomputed_bytes += bytes
		src_snap = prepare(grabsrc)()
		for x in dests:
			self.failUnlessEqual(x.prepare(dstcount).first(), stdrowcount)
			self.failUnlessEqual(x.prepare(grabdst)(), src_snap)
		self.failUnlessEqual(stdrowcount + count_offset, recomputed_messages)
		self.failUnlessEqual(recomputed_bytes, sp.total_bytes)

	@pg_tmp
	def testMultipleStatements(self):
		self.multiple_destinations()

	@pg_tmp
	def testMultipleStatementsBinary(self):
		self.multiple_destinations(binary = True)

	@pg_tmp
	def testMultipleStatementsSmallBuffer(self):
		self.multiple_destinations(buffer_size = 11)

	@pg_tmp
	def testNotices(self):
		# Inject a Notices directly into the stream to emulate
		# cases of asynchronous messages received during COPY.
		notices = [
			Notice((
				(b'S', b'NOTICE'),
				(b'C', b'00000'),
				(b'M', b'It\'s a beautiful day.'),
			)),
			Notice((
				(b'S', b'WARNING'),
				(b'C', b'01X1X1'),
				(b'M', b'FAILURE IS CERTAIN'),
			))
		]
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		# hook for notices..
		rmessages = []
		def hook(msg):
			rmessages.append(msg)
			# suppress
			return True
		stmt = prepare(srcsql)
		stmt.msghook = hook
		sp = Injector(notices, stmt, buffer_size = 133)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))
		seen_in_loop = 0
		with copyman.CopyManager(sp, sr) as copy:
			for x in copy:
				if rmessages:
					# Should get hooked before the COPY is over.
					seen_in_loop += 1
		self.failUnless(seen_in_loop > 0)
		self.failUnlessEqual(dst.prepare(dstcount).first(), stdrowcount)
		self.failUnlessEqual(dst.prepare(grabdst)(), prepare(grabsrc)())
		# The injector adds then everytime the wire data is confiscated
		# from the protocol connection.
		notice, warning = rmessages[:2]
		self.failUnlessEqual(notice.code, "00000")
		self.failUnlessEqual(warning.code, "01X1X1")
		self.failUnlessEqual(warning.details['severity'], "WARNING")
		self.failUnlessEqual(notice.message, "It's a beautiful day.")
		self.failUnlessEqual(warning.message, "FAILURE IS CERTAIN")
		self.failUnlessEqual(notice.details['severity'], "NOTICE")

	@pg_tmp
	def testAsyncNotify(self):
		# Inject a NOTIFY directly into the stream to emulate
		# cases of asynchronous messages received during COPY.
		notify = [Notify(1234, b'channel', b'payload')]
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		stmt = prepare(srcsql)
		sp = Injector(notify, prepare(srcsql), buffer_size = 133)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))
		seen_in_loop = 0
		with copyman.CopyManager(sp, sr) as copy:
			for x in copy:
				r = list(db.wait(0))
				if r:
					break
			else:
				self.fail("didn't pickup notify during copy")
		# Got the injected NOTIFY's, right?
		self.failUnlessEqual(r, [('channel', 'payload', 1234)])

	@pg_tmp
	def testRaiseInCopy(self):
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 128)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))
		i = 0
		class ThisError(Exception):
			pass
		try:
			with copyman.CopyManager(sp, sr) as copy:
				for x in copy:
					# Note, the state of the receiver has changed.
					# We may not be on a message boundary, so this test
					# exercises cases where an interrupt occurs where
					# re-alignment *may* need to occur.
					raise ThisError()
		except copyman.CopyFail as cf:
			# It's a copy failure, but due to ThisError.
			self.failUnless(isinstance(cf.__context__, ThisError))
		else:
			self.fail("didn't raise CopyFail")
		# Connections should be usable.
		self.failUnlessEqual(prepare('select 1').first(), 1)
		self.failUnlessEqual(dst.prepare('select 1').first(), 1)

	@pg_tmp
	def testRaiseInCopyOnEnter(self):
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 128)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))
		i = 0
		class ThatError(Exception):
			pass
		try:
			with copyman.CopyManager(sp, sr) as copy:
				raise ThatError()
		except copyman.CopyFail as cf:
			# yeah; error on incomplete COPY
			self.failUnless(isinstance(cf.__context__, ThatError))
		else:
			self.fail("didn't raise CopyFail")

	@pg_tmp
	def testCopyWithFailure(self):
		sqlexec(stdsource)
		dst = new()
		dst2 = new()
		dst.execute(stddst)
		dst2.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 128)
		sr1 = copyman.StatementReceiver(dst.prepare(dstsql))
		sr2 = copyman.StatementReceiver(dst2.prepare(dstsql))
		done = False
		with copyman.CopyManager(sp, sr1, sr2) as copy:
			while True:
				try:
					for x in copy:
						if not done:
							done = True
							dst2.pq.socket.close()
					else:
						# Done with copy.
						break
				except copyman.Fault as cf:
					if sr2 not in cf.faults:
						raise
		self.failUnless(done)
		self.failUnlessRaises(Exception, dst2.execute, 'select 1')
		self.failUnlessEqual(dst.prepare(dstcount).first(), stdrowcount)
		self.failUnlessEqual(dst.prepare(grabdst)(), prepare(grabsrc)())

	@pg_tmp
	def testEmptyRows(self):
		sqlexec(emptysource)
		dst = new()
		dst.execute(emptydst)
		sp = copyman.StatementProducer(prepare("COPY emptysource TO STDOUT"), buffer_size = 127)
		sr = copyman.StatementReceiver(dst.prepare("COPY empty FROM STDIN"))
		m = 0
		b = 0
		with copyman.CopyManager(sp, sr) as copy:
			for x in copy:
				nmsg, nbytes = x
				m += nmsg
				b += nbytes
		self.failUnlessEqual(m, 10)
		self.failUnlessEqual(prepare("SELECT COUNT(*) FROM emptysource").first(), 10)
		self.failUnlessEqual(dst.prepare("SELECT COUNT(*) FROM empty").first(), 10)
		self.failUnlessEqual(sr.count(), 10)
		self.failUnlessEqual(sp.count(), 10)

	@pg_tmp
	def testCopyOne(self):
		from io import BytesIO
		b = BytesIO()
		copyman.COPY(
			prepare('COPY (SELECT 1) TO STDOUT'),
			copyman.CallReceiver(b.writelines)
		)
		b.seek(0)
		self.failUnlessEqual(b.read(), b'1\n')

	@pg_tmp
	def testCopyNone(self):
		from io import BytesIO
		b = BytesIO()
		copyman.COPY(
			prepare('COPY (SELECT 1 LIMIT 0) TO STDOUT'),
			copyman.CallReceiver(b.writelines)
		)
		b.seek(0)
		self.failUnlessEqual(b.read(), b'')

	@pg_tmp
	def testNoReceivers(self):
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql))
		sr1 = copyman.StatementReceiver(dst.prepare(dstsql))
		done = False
		try:
			with copyman.CopyManager(sp, sr1) as copy:
				while True:
					try:
						for x in copy:
							if not done:
								done = True
								dst.pq.socket.close()
							else:
								self.fail("failed to detect dead socket")
					except copyman.Fault as cf:
						self.failUnless(sr1 in cf.faults)
						# Don't reconcile.
		except copyman.NoReceivers:
			# Success.
			pass
		else:
			self.fail("did not raise expected error")
		# Let the exception cause a failure.
		self.failUnless(done)

	@pg_tmp
	def testReconciliation(self):
		# cm.reconcile() test.
		sqlexec(stdsource)
		dst = new()
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 201)
		sr = copyman.StatementReceiver(dst.prepare(dstsql))

		original_call = sr.send
		class RecoverableError(Exception):
			pass
		def failed_write(*args):
			sr.send = original_call
			raise RecoverableError()
		sr.send = failed_write

		done = False
		recomputed_messages = 0
		recomputed_bytes = 0
		with copyman.CopyManager(sp, sr) as copy:
			while copy.receivers:
				try:
					for nmsg, nbytes in copy:
						recomputed_messages += nmsg
						recomputed_bytes += nbytes
					else:
						# Done with COPY, break out of while copy.receivers.
						break
				except copyman.Fault as cf:
					if isinstance(cf.faults[sr], RecoverableError):
						if done is True:
							self.fail("failed_write was called twice?")
						done = True
						self.failUnlessEqual(len(copy.receivers), 0)
						copy.reconcile(sr)
						self.failUnlessEqual(len(copy.receivers), 1)

		self.failUnlessEqual(done, True)

		# Connections should be usable.
		self.failUnlessEqual(prepare('select 1').first(), 1)
		self.failUnlessEqual(dst.prepare('select 1').first(), 1)
		# validate completion
		self.failUnlessEqual(stdrowcount, recomputed_messages)
		self.failUnlessEqual(recomputed_bytes, sp.total_bytes)
		self.failUnlessEqual(dst.prepare(dstcount).first(), stdrowcount)

	@pg_tmp
	def testDroppedConnection(self):
		# no cm.reconcile() test.
		sqlexec(stdsource)
		dst = new()
		dst2 = new()
		dst2.execute(stddst)
		dst.execute(stddst)
		sp = copyman.StatementProducer(prepare(srcsql), buffer_size = 201)
		sr1 = copyman.StatementReceiver(dst.prepare(dstsql))
		sr2 = copyman.StatementReceiver(dst2.prepare(dstsql))

		class TheCause(Exception):
			pass
		def failed_write(*args):
			raise TheCause()
		sr2.send = failed_write

		done = False
		recomputed_messages = 0
		recomputed_bytes = 0
		with copyman.CopyManager(sp, sr1, sr2) as copy:
			while copy.receivers:
				try:
					for nmsg, nbytes in copy:
						recomputed_messages += nmsg
						recomputed_bytes += nbytes
					else:
						# Done with COPY, break out of while copy.receivers.
						break
				except copyman.Fault as cf:
					self.failUnless(isinstance(cf.faults[sr2], TheCause))
					if done is True:
						self.fail("failed_write was called twice?")
					done = True
					self.failUnlessEqual(len(copy.receivers), 1)
					dst2.pq.socket.close()
					# We don't reconcile, so the manager only has one target now.

		self.failUnlessEqual(done, True)
		# May not be aligned; really, we're expecting the connection to
		# have died.
		self.failUnlessRaises(Exception, dst2.execute, "SELECT 1")

		# Connections should be usable.
		self.failUnlessEqual(prepare('select 1').first(), 1)
		self.failUnlessEqual(dst.prepare('select 1').first(), 1)
		# validate completion
		self.failUnlessEqual(stdrowcount, recomputed_messages)
		self.failUnlessEqual(recomputed_bytes, sp.total_bytes)
		self.failUnlessEqual(dst.prepare(dstcount).first(), stdrowcount)
		self.failUnlessEqual(sp.count(), stdrowcount)
		self.failUnlessEqual(sp.command(), "COPY")

from ..copyman import WireState
class test_WireState(unittest.TestCase):
	def testNormal(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd\x00\x00\x00\x04'))
		self.failUnlessEqual(messages, 1)
		self.failUnlessEqual(WS.remaining_bytes, 0)
		self.failUnlessEqual(WS.size_fragment, b'')
		self.failUnlessEqual(WS.final_view, None)

	def testIncomplete(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd\x00\x00\x00\x05'))
		self.failUnlessEqual(messages, 0)
		self.failUnlessEqual(WS.remaining_bytes, 1)
		self.failUnlessEqual(WS.size_fragment, b'')
		self.failUnlessEqual(WS.final_view, None)
		messages = WS.update(memoryview(b'x'))
		self.failUnlessEqual(messages, 1)
		self.failUnlessEqual(WS.remaining_bytes, 0)
		self.failUnlessEqual(WS.size_fragment, b'')
		self.failUnlessEqual(WS.final_view, None)

	def testIncompleteHeader_0size(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd'))
		self.failUnlessEqual(messages, 0)
		self.failUnlessEqual(WS.remaining_bytes, -1)
		self.failUnlessEqual(WS.size_fragment, b'')
		self.failUnlessEqual(WS.final_view, None)
		messages = WS.update(b'\x00\x00\x00\x04')
		self.failUnlessEqual(messages, 1)

	def testIncompleteHeader_1size(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd\x00'))
		self.failUnlessEqual(messages, 0)
		self.failUnlessEqual(WS.size_fragment, b'\x00')
		self.failUnlessEqual(WS.final_view, None)
		self.failUnlessEqual(WS.remaining_bytes, -1)
		messages = WS.update(memoryview(b'\x00\x00\x04'))
		self.failUnlessEqual(messages, 1)
		self.failUnlessEqual(WS.remaining_bytes, 0)

	def testIncompleteHeader_2size(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd\x00\x00'))
		self.failUnlessEqual(messages, 0)
		self.failUnlessEqual(WS.remaining_bytes, -1)
		self.failUnlessEqual(WS.size_fragment, b'\x00\x00')
		self.failUnlessEqual(WS.final_view, None)
		messages = WS.update(b'\x00\x04')
		self.failUnlessEqual(messages, 1)
		self.failUnlessEqual(WS.remaining_bytes, 0)

	def testIncompleteHeader_3size(self):
		WS=WireState()
		messages = WS.update(memoryview(b'd\x00\x00\x00'))
		self.failUnlessEqual(messages, 0)
		self.failUnlessEqual(WS.remaining_bytes, -1)
		self.failUnlessEqual(WS.size_fragment, b'\x00\x00\x00')
		self.failUnlessEqual(WS.final_view, None)
		messages = WS.update(memoryview(b'\x04'))
		self.failUnlessEqual(messages, 1)
		self.failUnlessEqual(WS.remaining_bytes, 0)

if __name__ == '__main__':
	unittest.main()
