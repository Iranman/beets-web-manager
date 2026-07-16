import { alpha, createTheme } from '@mui/material/styles';

const graphite = {
  bg: '#13161b',
  surface: '#1c2027',
  surface2: '#252b35',
  surface3: '#2d3440',
  border: '#414a58',
  borderSoft: 'rgba(255,255,255,0.13)',
  text: '#f4f4f5',
  muted: '#b8bdc7',
  muted2: '#8e95a3',
};

const red = {
  main: '#b91c1c',
  hover: '#ef4444',
  dark: '#7f1d1d',
};

const button = {
  primaryBg: red.main,
  primaryHover: red.hover,
  primaryText: '#ffffff',
  secondaryBg: '#1f2530',
  secondaryHover: '#2d3440',
  secondaryText: '#f8fafc',
  secondaryBorder: '#a8b0bd',
  disabledBg: '#3a424f',
  disabledText: '#eef2f7',
  disabledBorder: '#9aa3b2',
  dangerBg: '#991b1b',
  dangerHover: '#dc2626',
  dangerText: '#ffffff',
  warningBg: '#78350f',
  warningHover: '#92400e',
  warningText: '#fff7ed',
  warningBorder: '#f59e0b',
  focusRing: '#f87171',
};

// Medium-dark graphite theme with clear red accents for brand, primary actions, and active states.
export const theme = createTheme({
  palette: {
    mode: 'dark',
    background: {
      default: graphite.bg,
      paper: graphite.surface2,
    },
    primary: {
      main: red.main,
      light: red.hover,
      dark: red.dark,
      contrastText: '#ffffff',
    },
    secondary: {
      main: graphite.muted,
      contrastText: graphite.bg,
    },
    error: {
      main: '#f87171',
    },
    warning: {
      main: '#f59e0b',
    },
    success: {
      main: '#4ade80',
    },
    info: {
      main: '#60a5fa',
    },
    divider: graphite.border,
    text: {
      primary: graphite.text,
      secondary: graphite.muted,
      disabled: graphite.muted2,
    },
  },
  shape: {
    borderRadius: 6,
  },
  typography: {
    fontFamily:
      'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize: 13,
    h1: { fontSize: '1.45rem', fontWeight: 650, letterSpacing: 0 },
    h2: { fontSize: '1.18rem', fontWeight: 650, letterSpacing: 0 },
    h3: { fontSize: '1rem', fontWeight: 650, letterSpacing: 0 },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: graphite.bg, minHeight: '100vh' },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          borderRadius: 6,
          border: `1px solid ${graphite.borderSoft}`,
          backgroundImage: 'none',
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: {
          border: `1px solid ${graphite.border}`,
          backgroundColor: graphite.surface2,
          backgroundImage: 'none',
          boxShadow: 'none',
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          backgroundImage: 'none',
        },
      },
    },
    MuiTextField: {
      defaultProps: { size: 'small' },
    },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          color: graphite.text,
          backgroundColor: graphite.surface,
          '& .MuiOutlinedInput-notchedOutline': { borderColor: graphite.border },
          '&:hover .MuiOutlinedInput-notchedOutline': { borderColor: graphite.muted2 },
          '&.Mui-focused .MuiOutlinedInput-notchedOutline': { borderColor: red.main },
          '&.Mui-disabled': { backgroundColor: alpha(graphite.surface3, 0.72) },
        },
        input: {
          '&::placeholder': { color: graphite.muted2, opacity: 1 },
        },
      },
    },
    MuiInputLabel: {
      styleOverrides: {
        root: {
          color: graphite.muted,
          '&.Mui-focused': { color: red.hover },
        },
      },
    },
    MuiButton: {
      defaultProps: { size: 'small', disableElevation: true },
      styleOverrides: {
        root: {
          borderRadius: 6,
          textTransform: 'none',
          fontWeight: 650,
          '&.Mui-focusVisible': {
            boxShadow: `0 0 0 3px ${alpha(button.focusRing, 0.45)}`,
          },
          '&.Mui-disabled': {
            opacity: 1,
          },
          '&.MuiButton-contained': {
            backgroundColor: button.primaryBg,
            border: `1px solid ${button.primaryBg}`,
            color: button.primaryText,
            '&:hover': { backgroundColor: button.primaryHover, borderColor: button.primaryHover },
            '&.Mui-disabled': {
              backgroundColor: button.disabledBg,
              borderColor: button.disabledBorder,
              color: button.disabledText,
            },
          },
          '&.MuiButton-containedError': {
            backgroundColor: button.dangerBg,
            borderColor: button.dangerBg,
            color: button.dangerText,
            '&:hover': { backgroundColor: button.dangerHover, borderColor: button.dangerHover },
          },
          '&.MuiButton-containedWarning': {
            backgroundColor: button.warningBg,
            borderColor: button.warningBorder,
            color: button.warningText,
            '&:hover': { backgroundColor: button.warningHover, borderColor: button.warningBorder },
          },
          '&.MuiButton-outlined': {
            backgroundColor: button.secondaryBg,
            borderColor: button.secondaryBorder,
            color: button.secondaryText,
            '&:hover': {
              backgroundColor: button.secondaryHover,
              borderColor: red.hover,
              color: button.secondaryText,
            },
            '&.Mui-disabled': {
              backgroundColor: button.disabledBg,
              borderColor: button.disabledBorder,
              color: button.disabledText,
            },
          },
          '&.MuiButton-outlinedWarning': {
            backgroundColor: button.warningBg,
            borderColor: button.warningBorder,
            color: button.warningText,
            '&:hover': {
              backgroundColor: button.warningHover,
              borderColor: button.warningBorder,
              color: button.warningText,
            },
            '&.Mui-disabled': {
              backgroundColor: button.disabledBg,
              borderColor: button.disabledBorder,
              color: button.disabledText,
            },
          },
          '&.MuiButton-outlinedError': {
            backgroundColor: alpha(button.dangerBg, 0.24),
            borderColor: '#fca5a5',
            color: '#fecaca',
            '&:hover': {
              backgroundColor: alpha(button.dangerBg, 0.38),
              borderColor: '#f87171',
              color: '#fee2e2',
            },
          },
          '&.MuiButton-text': {
            color: graphite.text,
            '&:hover': { color: red.hover, backgroundColor: alpha(red.main, 0.1) },
            '&.Mui-disabled': {
              backgroundColor: button.disabledBg,
              border: `1px solid ${button.disabledBorder}`,
              color: button.disabledText,
            },
          },
          '&.MuiButton-textWarning': {
            backgroundColor: alpha(button.warningBg, 0.18),
            color: button.warningText,
            '&:hover': { backgroundColor: alpha(button.warningHover, 0.38), color: button.warningText },
          },
          '&.MuiButton-textError': {
            color: '#fecaca',
            '&:hover': { backgroundColor: alpha(button.dangerBg, 0.26), color: '#fee2e2' },
          },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          borderRadius: 6,
          fontFamily: 'inherit',
          fontWeight: 600,
          backgroundColor: graphite.surface3,
        },
        outlined: {
          borderColor: graphite.border,
        },
      },
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: {
          height: 4,
          borderRadius: 999,
          backgroundColor: graphite.surface3,
        },
        bar: {
          backgroundColor: red.hover,
        },
      },
    },
    MuiMenu: {
      styleOverrides: {
        paper: {
          border: `1px solid ${graphite.border}`,
          backgroundColor: graphite.surface2,
        },
      },
    },
    MuiMenuItem: {
      styleOverrides: {
        root: {
          '&:hover': { backgroundColor: alpha(red.main, 0.12) },
          '&.Mui-selected': { backgroundColor: alpha(red.main, 0.18) },
        },
      },
    },
  },
});
