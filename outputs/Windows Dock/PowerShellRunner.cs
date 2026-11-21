using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

// Runs a single script in a Windows job before any of its code can execute.
public static class PowerShellRunner
{
    private const uint CreateSuspended = 0x00000004;
    private const uint UseStandardHandles = 0x00000100;
    private const uint KillOnJobClose = 0x00002000;
    private const uint WaitTimeout = 258;
    private const ulong TicksPerMillisecond = 10000;
    private const int BasicAccountingInformation = 1;
    private const int ExtendedLimitInformation = 9;

    public sealed class Result
    {
        public readonly bool TimedOut;
        public readonly int ExitCode;

        internal Result(bool timedOut, int exitCode)
        {
            TimedOut = timedOut;
            ExitCode = exitCode;
        }
    }

    public static Result Run(string executable, string directory, uint timeoutMilliseconds)
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        Check(job != IntPtr.Zero, "Creazione del job PowerShell");
        var process = new ProcessInformation();
        bool assigned = false;
        try
        {
            var limits = new ExtendedLimits();
            limits.Basic.LimitFlags = KillOnJobClose;
            Check(SetInformationJobObject(job, ExtendedLimitInformation, ref limits,
                Marshal.SizeOf(typeof(ExtendedLimits))), "Configurazione del job PowerShell");

            var startup = new StartupInformation {
                Size = Marshal.SizeOf(typeof(StartupInformation)),
                Flags = UseStandardHandles,
                StandardInput = GetStdHandle(-10),
                StandardOutput = GetStdHandle(-11),
                StandardError = GetStdHandle(-12)
            };
            var command = new StringBuilder("\"" + executable + "\" -NoLogo -NoProfile -NonInteractive "
                + "-ExecutionPolicy RemoteSigned -File .\\script.ps1");
            Check(CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, true,
                CreateSuspended, IntPtr.Zero, directory, ref startup, out process), "Avvio di PowerShell");
            Check(AssignProcessToJobObject(job, process.Process), "Assegnazione di PowerShell al job");
            assigned = true;
            ulong start;
            FileTime startUtc;
            do
            {
                start = InterruptTime();
                GetSystemTimePreciseAsFileTime(out startUtc);
            }
            while (InterruptTime() - start > TicksPerMillisecond);
            ulong duration = timeoutMilliseconds * TicksPerMillisecond;
            Check(ResumeThread(process.Thread) != UInt32.MaxValue, "Ripresa di PowerShell");

            uint finished = WaitForScript(process.Process, start + duration, startUtc.Ticks + duration);
            if (finished == WaitTimeout)
                return new Result(true, 124);
            Check(finished == 0, "Attesa di PowerShell");

            uint code;
            Check(GetExitCodeProcess(process.Process, out code), "Lettura del codice PowerShell");
            return new Result(false, unchecked((int)code));
        }
        finally
        {
            try
            {
                if (assigned)
                    StopJob(job);
                else if (process.Process != IntPtr.Zero)
                    TerminateProcess(process.Process, 1);
            }
            finally
            {
                if (process.Thread != IntPtr.Zero)
                    CloseHandle(process.Thread);
                if (process.Process != IntPtr.Zero)
                    CloseHandle(process.Process);
                // Closing this handle is also the fallback if the supervisor is interrupted.
                CloseHandle(job);
            }
        }
    }

    private static uint WaitForScript(IntPtr process, ulong deadline, ulong deadlineUtc)
    {
        // The clock includes suspension, unlike the relative Win32 wait on its own.
        while (true)
        {
            ulong now = InterruptTime();
            ulong remaining = now >= deadline ? 0
                : (deadline - now + TicksPerMillisecond - 1) / TicksPerMillisecond;
            uint status = WaitForSingleObject(process, (uint)Math.Min(100UL, remaining));
            if (status == WaitTimeout && remaining != 0)
                continue;
            if (status != 0 || InterruptTime() < deadline)
                return status;

            // The supervisor can observe a signaled process after the deadline.
            FileTime created, exited, kernel, user;
            Check(GetProcessTimes(process, out created, out exited, out kernel, out user),
                "Lettura dell'ora di uscita di PowerShell");
            return exited.Ticks <= deadlineUtc ? status : WaitTimeout;
        }
    }

    private static ulong InterruptTime()
    {
        ulong time;
        QueryInterruptTimePrecise(out time);
        return time;
    }

    private static void StopJob(IntPtr job)
    {
        Check(TerminateJobObject(job, 124), "Arresto del job PowerShell");
        var clock = Stopwatch.StartNew();
        while (true)
        {
            BasicAccounting accounting;
            Check(QueryInformationJobObject(job, BasicAccountingInformation, out accounting,
                Marshal.SizeOf(typeof(BasicAccounting)), IntPtr.Zero), "Verifica del job PowerShell");
            if (accounting.ActiveProcesses == 0)
                return;
            if (clock.ElapsedMilliseconds >= 10000)
                throw new InvalidOperationException("Arresto dei processi PowerShell non confermato.");
            Thread.Sleep(20);
        }
    }

    private static void Check(bool success, string operation)
    {
        if (!success)
        {
            int code = Marshal.GetLastWin32Error();
            throw new Win32Exception(code, operation + ": " + new Win32Exception(code).Message);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimits
    {
        public long PerProcessUserTime, PerJobUserTime;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperations, WriteOperations, OtherOperations;
        public ulong ReadBytes, WriteBytes, OtherBytes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimits
    {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemory, PeakJobMemory;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicAccounting
    {
        public long TotalUserTime, TotalKernelTime, PeriodUserTime, PeriodKernelTime;
        public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInformation
    {
        public int Size;
        public string Reserved, Desktop, Title;
        public uint X, Y, XSize, YSize, XChars, YChars, FillAttribute, Flags;
        public ushort ShowWindow, ReservedBytes;
        public IntPtr ReservedData, StandardInput, StandardOutput, StandardError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr Process, Thread;
        public uint ProcessId, ThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint Low, High;

        public ulong Ticks { get { return ((ulong)High << 32) | Low; } }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits limits, int size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool QueryInformationJobObject(IntPtr job, int infoClass, out BasicAccounting accounting,
        int size, IntPtr returnedSize);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string application, StringBuilder command, IntPtr processAttributes,
        IntPtr threadAttributes, bool inheritHandles, uint flags, IntPtr environment, string directory,
        ref StartupInformation startup, out ProcessInformation process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("api-ms-win-core-realtime-l1-1-1.dll")]
    private static extern void QueryInterruptTimePrecise(out ulong time);

    [DllImport("kernel32.dll")]
    private static extern void GetSystemTimePreciseAsFileTime(out FileTime time);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetProcessTimes(IntPtr process, out FileTime created, out FileTime exited,
        out FileTime kernel, out FileTime user);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetStdHandle(int id);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);
}
