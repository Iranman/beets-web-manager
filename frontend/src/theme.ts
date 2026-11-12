import { alpha, createTheme } from '@mui/material/styles';

// Record shelf palette: mirrors the tokens in styles.css (warm charcoal surfaces, brass accent).
const graphite = {
  bg: '#171310',
  surface: '#201a16',
  surface2: '#2a231d',
  surface3: '#342c25',
  border: '#4b4137',
  // >= 3:1 against bg..surface-3 (#606).
  fieldBorder: '#8f8170',
  borderSoft: 'rgba(246,222,180,0.13)',
  text: '#f6f0e7',
  muted: '#c4b5a2',
  muted2: '#a39481',
};

// Brass accent for brand, primary actions and active states. Dark text on it (>= 4.5:1).
const brass = {
  main: '#d39e3f',
  hover: '#e2b25c',
  dark: '#b5822b',
  text: '#1a140e',
};

const displayFont = '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif';

const button = {
  primaryBg: brass.main,
  primaryHover: brass.hover,
  primaryText: brass.text,
  secondaryBg: '#241e19',
  secondaryHover: '#342c25',
  secondaryText: '#f8f1e7',
  secondaryBorder: '#b3a491',
  // Disabled (#610): recessed fill, soft border, muted label (>= 4.5:1 on its fill);
  // reads as unavailable, never as primary. Literal hex: tests/test_import_review_button_contrast.py parses these.
  disabledBg: '#201a16',
  disabledText: '#a39481',
  disabledBorder: '#4b4137',
  dangerBg: '#991b1b',
  dangerHover: '#dc2626',
  dangerText: '#ffffff',
  warningBg: '#78350f',
  warningHover: '#92400e',
  warningText: '#fff7ed',
  warningBorder: '#f59e0b',
  focusRing: '#f0c46a',
};

const disabledButton = {
  backgroundColor: button.disabledBg,
  borderColor: button.disabledBorder,
  color: button.disabledText,
};

// Warm dark record-shelf theme with a brass accent for brand, primary actions, and active states.
export const theme = createTheme({
  palette: {
    mode: 'dark',
    background: {
      default: graphite.bg,
      paper: graphite.surface2,
    },
    primary: {
      main: brass.main,
      light: brass.hover,
      dark: brass.dark,
      contrastText: brass.text,
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
    h1: { fontFamily: displayFont, fontSize: '1.5rem', fontWeight: 600, letterSpacing: 0 },
    h2: { fontFamily: displayFont, fontSize: '1.22rem', fontWeight: 600, letterSpacing: 0 },
    h3: { fontSize: '1rem', fontWeight: 600, letterSpacing: 0 },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        body: { backgroundColor: graphite.bg, minHeight: '100vh' },
      },
    },
    // Solid 2px ring on keyboard focus for every ButtonBase (buttons, icon buttons, tabs, menu items).
    MuiButtonBase: {
      styleOverrides: {
        root: {
          '&.Mui-focusVisible': {
            outline: `2px solid ${button.focusRing}`,
            outlineOffset: 2,
          },
        },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          borderRadius: 6,
          border: `1px solid ${graphite.borderSoft}`,
          backgroundImage: 'none',
          // Info sits on the warm shelf surface; blue stays on the icon only, so notices
          // read as neutral messages rather than a navy panel.
          variants: [
            {
              props: { variant: 'standard', severity: 'info' },
              style: {
                backgroundColor: graphite.surface2,
                color: graphite.text,
                '& .MuiAlert-icon': { color: '#93c5fd' },
              },
            },
          ],
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
          '& .MuiOutlinedInput-notchedOutline': { borderColor: graphite.fieldBorder },
          '&:hover .MuiOutlinedInput-notchedOutline': { borderColor: graphite.muted2 },
          '&.Mui-focused .MuiOutlinedInput-notchedOutline': { borderColor: brass.main },
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
          '&.Mui-focused': { color: brass.hover },
        },
      },
    },
    MuiButton: {
      defaultProps: { size: 'small', disableElevation: true },
      styleOverrides: {
        root: {
          borderRadius: 6,
          textTransform: 'none',
          fontWeight: 600,
          // Labels stay on one line at phone widths (#607); containers wrap instead.
          whiteSpace: 'nowrap',
          minHeight: 32,
          // Hierarchy (#610): contained = the one primary action, outlined = secondary,
          // text = tertiary (navigation, shortcuts, dismiss). Disabled looks the same in every variant.
          '&.Mui-disabled': {
            opacity: 1,
          },
          // MUI sets pointer-events: none on disabled; re-enable on native buttons only so the cursor shows.
          'button&.Mui-disabled': { pointerEvents: 'auto', cursor: 'not-allowed' },
          '&.MuiButton-contained': {
            backgroundColor: button.primaryBg,
            border: `1px solid ${button.primaryBg}`,
            color: button.primaryText,
            '&:hover': { backgroundColor: button.primaryHover, borderColor: button.primaryHover },
            '&:active': { backgroundColor: brass.dark, borderColor: brass.dark },
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
              borderColor: brass.hover,
              color: button.secondaryText,
            },
            '&:active': { backgroundColor: graphite.surface2 },
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
            '&:hover': { color: brass.hover, backgroundColor: alpha(brass.main, 0.1) },
            '&:active': { backgroundColor: alpha(brass.main, 0.2) },
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
          // Declared last at the same specificity as the variant :hover/:active rules, so no
          // variant or colour beats it (hover is reachable because the cursor rule restores pointer events).
          '&.MuiButton-root.Mui-disabled': disabledButton,
          '&.MuiButton-text.Mui-disabled': { backgroundColor: 'transparent' },
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
          backgroundColor: brass.hover,
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
          '&:hover': { backgroundColor: alpha(brass.main, 0.12) },
          '&.Mui-selected': { backgroundColor: alpha(brass.main, 0.18) },
        },
      },
    },
  },
});
